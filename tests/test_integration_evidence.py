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


def _gh_result(state="MERGED", head="abc123", merged_at="2026-09-19T00:00:00Z",
               repository="example/repo", base_branch="main"):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=0,
        stdout=json.dumps({
            "url": ARTIFACT[0]["url"], "number": 42, "state": state,
            "mergedAt": merged_at, "headRefOid": head,
            "baseRepository": {"nameWithOwner": repository}, "baseRefName": base_branch,
        }),
        stderr="",
    )


def _mock_git_and_gh(monkeypatch, gh_result):
    def run(args, **kwargs):
        if args[0] == "gh":
            return gh_result
        if args[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, "git@github.com:example/repo.git\n", "")
        if "symbolic-ref" in args:
            return subprocess.CompletedProcess(args, 0, "origin/main\n", "")
        raise AssertionError(args)
    monkeypatch.setattr(subprocess, "run", run)


def test_verified_pull_request_is_bound_to_reviewed_revision(tmp_path, monkeypatch):
    _mock_git_and_gh(monkeypatch, _gh_result())
    evidence = verify_pull_request_handoff(str(tmp_path), ARTIFACT, "abc123")
    assert evidence["state"] == "merged"
    assert evidence["head_revision"] == "abc123"
    assert evidence["external_id"] == "42"
    assert evidence["destination_repository"] == "example/repo"
    assert evidence["base_branch"] == "main"


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_gh_result(state="OPEN", merged_at=None), "has not been merged"),
        (_gh_result(head="different"), "does not match"),
    ],
)
def test_unmerged_or_wrong_revision_pr_is_rejected(tmp_path, monkeypatch, result, message):
    _mock_git_and_gh(monkeypatch, result)
    with pytest.raises(IntegrationEvidenceError, match=message):
        verify_pull_request_handoff(str(tmp_path), ARTIFACT, "abc123")


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_gh_result(repository="someone/fork"), "different repository"),
        (_gh_result(base_branch="release"), "different branch"),
    ],
)
def test_wrong_pull_request_destination_is_rejected(tmp_path, monkeypatch, result, message):
    _mock_git_and_gh(monkeypatch, result)
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
