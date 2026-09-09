"""Bundled adapters must reach installs that were seeded by an older release.

copy_bundled_adapters() used to return early whenever the user directory held
any YAML, so a directory seeded once was frozen forever and every later adapter
fix — corrected CLI flags included — never arrived.
"""

import shutil

import pytest
import yaml

from agent_kanban_pm.runtime import adapter_loader


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(adapter_loader, "BUNDLED_ADAPTERS_DIR", bundled)
    monkeypatch.setattr(adapter_loader, "USER_ADAPTERS_DIR", user)
    return bundled, user


def _write(path, name, version, command):
    path.write_text(
        yaml.safe_dump({
            "name": name,
            "display_name": name.title(),
            "version": version,
            "invoke": {"command": command},
        }),
        encoding="utf-8",
    )


def test_a_newer_bundled_adapter_replaces_the_stale_local_copy(dirs):
    bundled, user = dirs
    _write(bundled / "demo.yaml", "demo", "1.1", "new-command")
    _write(user / "demo.yaml", "demo", "1.0", "old-command")

    adapter_loader.copy_bundled_adapters()

    refreshed = yaml.safe_load((user / "demo.yaml").read_text(encoding="utf-8"))
    assert refreshed["version"] == "1.1"
    assert refreshed["invoke"]["command"] == "new-command"


def test_the_replaced_copy_is_backed_up(dirs):
    bundled, user = dirs
    _write(bundled / "demo.yaml", "demo", "2.0", "new-command")
    _write(user / "demo.yaml", "demo", "1.0", "old-command")

    adapter_loader.copy_bundled_adapters()

    backup = user / "demo.yaml.bak-1.0"
    assert backup.exists()
    assert yaml.safe_load(backup.read_text(encoding="utf-8"))["invoke"]["command"] == "old-command"


def test_a_local_adapter_at_or_ahead_of_the_bundled_version_is_kept(dirs):
    bundled, user = dirs
    _write(bundled / "demo.yaml", "demo", "1.0", "bundled-command")
    _write(user / "demo.yaml", "demo", "1.4", "my-own-command")

    adapter_loader.copy_bundled_adapters()

    kept = yaml.safe_load((user / "demo.yaml").read_text(encoding="utf-8"))
    assert kept["invoke"]["command"] == "my-own-command"
    assert not (user / "demo.yaml.bak-1.4").exists()


def test_a_missing_adapter_is_still_installed(dirs):
    bundled, user = dirs
    _write(bundled / "demo.yaml", "demo", "1.0", "bundled-command")

    adapter_loader.copy_bundled_adapters()

    assert (user / "demo.yaml").exists()


def test_an_unparseable_local_file_is_treated_as_oldest(dirs):
    bundled, user = dirs
    _write(bundled / "demo.yaml", "demo", "1.0", "bundled-command")
    (user / "demo.yaml").write_text("{ not: valid: yaml", encoding="utf-8")

    adapter_loader.copy_bundled_adapters()

    assert yaml.safe_load((user / "demo.yaml").read_text(encoding="utf-8"))["version"] == "1.0"
