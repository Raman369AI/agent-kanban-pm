"""Phase 2 security hardening regression tests.

Covers:
- /ui mutation endpoints require the Kanban token (closes the HTML-route
  auth bypass), and cookie-authenticated mutations additionally require the
  X-CSRF-Token header embedded in UI pages.
- /ui/api/* JSON endpoints require the token on every method.
- The auth cookie is HttpOnly + SameSite=strict.
- Host-header validation via TrustedHostMiddleware (DNS-rebinding defense).
- ~/.kanban/token is created owner-only (0600) and tightened on read.
- preferences.yaml autonomy knob: supervised by default, explicit auto opt-in.

The app under test is imported with KANBAN_TESTING=1 (conftest), which keeps
TrustedHostMiddleware out of the stack so TestClient's `testserver` host is
allowed; the token/CSRF checks read the env var per request, so the
`secure_client` fixture simply deletes it for the duration of a test.
"""

from __future__ import annotations

import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

import tests_helper  # noqa: F401  — autouse cleanup listeners

from agent_kanban_pm.app import app
from agent_kanban_pm.runtime.instance import (
    ALLOWED_HOSTS,
    get_auth_token,
    get_csrf_token,
    tokens_match,
)
from agent_kanban_pm.runtime.preferences import Preferences, RoleAssignment


@pytest.fixture
def secure_client(monkeypatch):
    """TestClient with the KANBAN_TESTING auth bypass switched off."""
    monkeypatch.delenv("KANBAN_TESTING", raising=False)
    with TestClient(app) as client:
        yield client


def _owner_headers(client: TestClient, token: str) -> dict[str, str]:
    response = client.get("/entities/me", headers={"X-Kanban-Token": token})
    assert response.status_code == 200, response.text
    owner = response.json()
    return {"X-Entity-ID": str(owner["id"])}


# ---------------------------------------------------------------------------
# /ui auth bypass
# ---------------------------------------------------------------------------


def test_html_page_get_sets_httponly_strict_cookie(secure_client):
    response = secure_client.get("/")
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "kanban-token=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_ui_mutation_without_token_is_rejected(secure_client):
    response = secure_client.post("/ui/projects/create", json={"name": "nope"})
    assert response.status_code == 401
    assert "Kanban Auth Token" in response.json()["detail"]


def test_ui_mutation_with_bad_token_is_rejected(secure_client):
    response = secure_client.post(
        "/ui/projects/create",
        json={"name": "nope"},
        headers={"X-Kanban-Token": "wrong-token"},
    )
    assert response.status_code == 401


def test_ui_delete_without_token_is_rejected(secure_client):
    # Middleware rejects before routing, so the missing task id is irrelevant.
    response = secure_client.delete("/ui/tasks/999999")
    assert response.status_code == 401


def test_ui_mutation_with_cookie_requires_csrf(secure_client):
    secure_client.cookies.set("kanban-token", get_auth_token())
    response = secure_client.post("/ui/projects/create", json={"name": "nope"})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_ui_mutation_with_wrong_csrf_is_rejected(secure_client):
    secure_client.cookies.set("kanban-token", get_auth_token())
    response = secure_client.post(
        "/ui/projects/create",
        json={"name": "nope"},
        headers={"X-CSRF-Token": "not-the-csrf-token"},
    )
    assert response.status_code == 403


def test_ui_mutation_with_cookie_and_csrf_succeeds(secure_client):
    token = get_auth_token()
    secure_client.cookies.set("kanban-token", token)
    response = secure_client.post(
        "/ui/projects/create",
        json={"name": "Sec Cookie Project"},
        headers={
            "X-CSRF-Token": get_csrf_token(),
            **_owner_headers(secure_client, token),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Sec Cookie Project"


def test_ui_mutation_with_header_token_skips_csrf(secure_client):
    token = get_auth_token()
    response = secure_client.post(
        "/ui/projects/create",
        json={"name": "Sec Header Project"},
        headers={"X-Kanban-Token": token, **_owner_headers(secure_client, token)},
    )
    assert response.status_code == 200, response.text


def test_ui_api_json_get_requires_token(secure_client):
    assert secure_client.get("/ui/api/settings").status_code == 401
    assert secure_client.get("/ui/api/folders").status_code == 401

    secure_client.cookies.set("kanban-token", get_auth_token())
    assert secure_client.get("/ui/api/settings").status_code == 200


def test_rest_api_still_requires_token(secure_client):
    assert secure_client.get("/tasks").status_code == 401
    secure_client.cookies.set("kanban-token", get_auth_token())
    assert secure_client.get("/tasks").status_code == 200


def test_health_and_static_stay_exempt(secure_client):
    assert secure_client.get("/health").status_code == 200
    assert secure_client.get("/static/css/style.css").status_code == 200


def test_board_page_embeds_csrf_token(secure_client):
    response = secure_client.get("/ui/projects")
    assert response.status_code == 200
    assert 'name="kanban-csrf-token"' in response.text
    assert get_csrf_token() in response.text


# ---------------------------------------------------------------------------
# Host header validation (TrustedHostMiddleware)
# ---------------------------------------------------------------------------


def test_trusted_host_middleware_rejects_foreign_hosts():
    inner = FastAPI()

    @inner.get("/")
    def _root():
        return {"ok": True}

    guarded = TrustedHostMiddleware(inner, allowed_hosts=ALLOWED_HOSTS, www_redirect=False)
    client = TestClient(guarded)
    assert client.get("/", headers={"host": "localhost:8000"}).status_code == 200
    assert client.get("/", headers={"host": "127.0.0.1:8123"}).status_code == 200
    assert client.get("/", headers={"host": "evil.example.com"}).status_code == 400
    assert client.get("/", headers={"host": "localhost.evil.com"}).status_code == 400


# ---------------------------------------------------------------------------
# Token file permissions
# ---------------------------------------------------------------------------


def test_auth_token_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    token = get_auth_token()
    token_file = tmp_path / ".kanban" / "token"
    assert token_file.exists()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert get_auth_token() == token  # stable across calls


def test_existing_token_file_permissions_are_tightened(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    token_dir = tmp_path / ".kanban"
    token_dir.mkdir()
    token_file = token_dir / "token"
    token_file.write_text("tok-abc123", encoding="utf-8")
    token_file.chmod(0o644)

    assert get_auth_token() == "tok-abc123"  # pre-existing token preserved
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_csrf_token_is_derived_from_auth_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first = get_csrf_token()
    assert first == get_csrf_token()
    (tmp_path / ".kanban" / "token").write_text("different-token", encoding="utf-8")
    assert get_csrf_token() != first


# ---------------------------------------------------------------------------
# Autonomy knob (supervised by default)
# ---------------------------------------------------------------------------


def test_autonomy_defaults_to_supervised():
    prefs = Preferences()
    assert prefs.autonomy_for_role("worker") == "supervised"
    assert prefs.autonomy_for_role(None) == "supervised"
    assert prefs.autonomy_for_role("unconfigured-role") == "supervised"
    assert RoleAssignment(agent="claude").autonomy == "supervised"


def test_autonomy_opt_in_and_unknown_values_fall_back_safely():
    prefs = Preferences()
    prefs.set_role_assignment("worker", RoleAssignment(agent="claude", autonomy="auto"))
    assert prefs.autonomy_for_role("worker") == "auto"

    # A typo in preferences.yaml must never unlock bypass flags.
    prefs.set_role_assignment("ui", RoleAssignment(agent="claude", autonomy="yolo"))
    assert prefs.autonomy_for_role("ui") == "supervised"


# ---------------------------------------------------------------------------
# Constant-time token comparison
# ---------------------------------------------------------------------------


def test_tokens_match_accepts_only_the_exact_token():
    token = get_auth_token()
    assert tokens_match(token, token) is True
    assert tokens_match(token + "x", token) is False
    assert tokens_match(token[:-1], token) is False
    assert tokens_match("wrong", token) is False


def test_tokens_match_rejects_empty_credentials_without_raising():
    token = get_auth_token()
    assert tokens_match(None, token) is False
    assert tokens_match("", token) is False


def test_tokens_match_rejects_non_ascii_without_raising():
    """Header values reach the app latin-1 decoded, so non-ASCII is reachable.

    hmac.compare_digest raises TypeError on non-ASCII str, which would turn a
    malformed token header into a 500 instead of a 401.
    """
    token = get_auth_token()
    assert tokens_match("t\xf6k\xe9n", token) is False
    assert tokens_match("\ud800lone-surrogate", token) is False
