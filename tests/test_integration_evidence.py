"""Provider-observed completion evidence for external integrations."""
import json
import subprocess

import pytest

from agent_kanban_pm.runtime.integration_evidence import (
    IntegrationEvidenceError,
    verify_pull_request_handoff,
)


ARTIFACT = [{
    "kind": "pull_request",
    "provider": "github",
    "url": "https://github.com/example/repo/pull/42",
}]


def _gh_result(state="MERGED", head="abc123", merged_at="2026-09-19T00:00:00Z"):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=0,
        stdout=json.dumps({
            "url": ARTIFACT[0]["url"], "number": 42, "state": state,
            "mergedAt": merged_at, "headRefOid": head,
        }),
        stderr="",
    )


def test_verified_pull_request_is_bound_to_reviewed_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _gh_result())
    evidence = verify_pull_request_handoff(str(tmp_path), ARTIFACT, "abc123")
    assert evidence["state"] == "merged"
    assert evidence["head_revision"] == "abc123"
    assert evidence["external_id"] == "42"


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_gh_result(state="OPEN", merged_at=None), "has not been merged"),
        (_gh_result(head="different"), "does not match"),
    ],
)
def test_unmerged_or_wrong_revision_pr_is_rejected(tmp_path, monkeypatch, result, message):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(IntegrationEvidenceError, match=message):
        verify_pull_request_handoff(str(tmp_path), ARTIFACT, "abc123")


@pytest.mark.parametrize("url", [
    "http://github.com/example/repo/pull/42",
    "https://example.com/example/repo/pull/42",
    "https://github.com/example/repo/issues/42",
])
def test_only_canonical_github_pull_request_urls_are_accepted(tmp_path, url):
    artifact = [{"kind": "pull_request", "provider": "github", "url": url}]
    with pytest.raises(IntegrationEvidenceError):
        verify_pull_request_handoff(str(tmp_path), artifact, "abc123")
