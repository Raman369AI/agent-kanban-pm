from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _workflow(name: str) -> dict:
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def test_release_workflow_uses_tag_trigger_and_isolated_oidc_publish_job():
    workflow = _workflow("release.yml")
    assert workflow["on"]["push"]["tags"] == ["v*"]
    publish = workflow["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    assert (
        publish["steps"][-1]["uses"]
        == "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
    )
    assert workflow["permissions"] == {"contents": "read"}


def test_release_workflow_has_distribution_and_post_publish_gates():
    workflow = _workflow("release.yml")
    assert workflow["jobs"]["publish"]["needs"] == "build"
    assert workflow["jobs"]["smoke"]["needs"] == "publish"
    assert workflow["jobs"]["github-release"]["needs"] == "smoke"


def test_ci_claimed_platforms_are_exercised():
    workflow = _workflow("python-package.yml")
    jobs = workflow["jobs"]
    assert jobs["test"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.12",
        "3.13",
    ]
    assert jobs["test-macos"]["runs-on"] == "macos-latest"


def test_release_docs_and_changelog_are_present_and_ordered():
    for filename in (
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "RELEASING.md",
    ):
        assert (ROOT / filename).is_file()

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.index("## [Unreleased]") < changelog.index("## [0.4.0rc1]")
