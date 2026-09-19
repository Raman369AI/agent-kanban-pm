"""Server-observed evidence for external integration outcomes."""
from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from urllib.parse import urlparse


class IntegrationEvidenceError(RuntimeError):
    """The submitted external artifact could not prove the required outcome."""


def _github_pull_url(value: object) -> str:
    if not isinstance(value, str):
        raise IntegrationEvidenceError("The git_pr handoff must include a GitHub pull request URL")
    parsed = urlparse(value.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise IntegrationEvidenceError("The pull request URL must use https://github.com")
    if len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
        raise IntegrationEvidenceError("The artifact URL is not a GitHub pull request")
    return value.strip()


def _github_repository(value: str) -> str | None:
    """Normalize a GitHub remote URL to owner/repository."""
    remote = (value or "").strip()
    if remote.startswith("git@github.com:"):
        path = remote.split(":", 1)[1]
    elif remote.startswith("ssh://git@github.com/"):
        path = urlparse(remote).path.lstrip("/")
    else:
        parsed = urlparse(remote)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) == 2 and all(parts) else None


def _project_destination(workspace_path: str) -> tuple[str, str]:
    workspace = Path(workspace_path)
    try:
        remote = subprocess.run(
            ["git", "-C", str(workspace), "remote", "get-url", "origin"],
            check=True, text=True, capture_output=True, timeout=10,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise IntegrationEvidenceError("The project must have a readable GitHub origin remote") from exc
    repository = _github_repository(remote)
    if repository is None:
        raise IntegrationEvidenceError("The project origin must identify one GitHub repository")

    try:
        symbolic = subprocess.run(
            ["git", "-C", str(workspace), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            check=False, text=True, capture_output=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise IntegrationEvidenceError("Could not determine the project's integration branch") from exc
    base_branch = ""
    if symbolic.returncode == 0 and symbolic.stdout.strip().startswith("origin/"):
        base_branch = symbolic.stdout.strip().removeprefix("origin/")
    if not base_branch:
        for candidate in ("main", "master"):
            result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"],
                check=False, text=True, capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                base_branch = candidate
                break
    if not base_branch:
        raise IntegrationEvidenceError("The project origin has no identifiable integration branch")
    return repository, base_branch


def verify_pull_request_handoff(
    workspace_path: str, artifacts: list[dict], expected_revision: str
) -> dict:
    """Verify a merged GitHub PR and bind it to the implementation revision."""
    candidates = [
        item for item in artifacts
        if isinstance(item, dict)
        and item.get("kind") == "pull_request"
        and item.get("provider", "github") == "github"
    ]
    if len(candidates) != 1:
        raise IntegrationEvidenceError(
            "The git_pr handoff must include exactly one GitHub pull_request artifact"
        )
    url = _github_pull_url(candidates[0].get("url"))
    expected_repository, expected_base_branch = _project_destination(workspace_path)
    try:
        result = subprocess.run(
            ["gh", "pr", "view", url, "--json",
             "url,number,state,mergedAt,headRefOid,baseRefName,baseRepository"],
            cwd=Path(workspace_path), check=True, text=True, capture_output=True, timeout=30,
        )
        observed = json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise IntegrationEvidenceError("GitHub CLI is required to verify the pull request") from exc
    except subprocess.TimeoutExpired as exc:
        raise IntegrationEvidenceError("Timed out while verifying the pull request with GitHub") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "GitHub rejected the lookup").strip()
        raise IntegrationEvidenceError(f"Could not verify the pull request: {detail}") from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise IntegrationEvidenceError("GitHub returned invalid pull request metadata") from exc

    if observed.get("state") != "MERGED" or not observed.get("mergedAt"):
        raise IntegrationEvidenceError("The pull request has not been merged")
    if observed.get("headRefOid") != expected_revision:
        raise IntegrationEvidenceError(
            "The merged pull request head does not match the reviewed implementation revision"
        )
    repository = (observed.get("baseRepository") or {}).get("nameWithOwner")
    if not isinstance(repository, str) or repository.lower() != expected_repository.lower():
        raise IntegrationEvidenceError(
            "The merged pull request targets a different repository than the project origin"
        )
    base_branch = observed.get("baseRefName")
    if base_branch != expected_base_branch:
        raise IntegrationEvidenceError(
            "The merged pull request targets a different branch than the project integration branch"
        )
    observed_url = _github_pull_url(observed.get("url"))
    if observed_url.rstrip("/") != url.rstrip("/"):
        raise IntegrationEvidenceError("GitHub returned a different pull request")
    return {
        "kind": "pull_request", "provider": "github", "url": observed_url,
        "external_id": str(observed.get("number")), "state": "merged",
        "merged_at": observed["mergedAt"], "head_revision": observed["headRefOid"],
        "destination_repository": repository, "base_branch": base_branch,
        "verified_at": datetime.now(UTC).isoformat(),
    }
