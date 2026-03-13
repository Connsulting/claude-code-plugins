import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = str(Path(__file__).parent.parent)
sys.path.insert(0, PLUGIN_ROOT)

from lib import bootstrap


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cache_clear = getattr(bootstrap._python_has_pip, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()
    yield
    cache_clear = getattr(bootstrap._python_has_pip, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()


def test_missing_packages_distinguishes_core_and_embedding(monkeypatch):
    available = {
        "sqlite_vec": False,
        "sentence_transformers": False,
        "pysqlite3": False,
    }

    monkeypatch.setattr(bootstrap, "_module_available", lambda name: available.get(name, False))
    monkeypatch.setattr(bootstrap, "_sqlite_module_supports_extensions", lambda module: False)
    monkeypatch.setattr(bootstrap, "_pysqlite_package_name", lambda: "pysqlite3-binary")

    assert bootstrap.missing_packages(bootstrap.CORE) == ["pysqlite3-binary", "sqlite-vec"]
    assert bootstrap.missing_packages(bootstrap.EMBEDDING) == ["sentence-transformers"]

    available["sqlite_vec"] = True
    assert bootstrap.missing_packages(bootstrap.CORE) == ["pysqlite3-binary"]


def test_dependency_ready_ignores_broken_legacy_plugin_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", list(sys.path))

    legacy_plugin_dir = tmp_path / ".claude" / "plugins"
    legacy_plugin_dir.mkdir(parents=True)
    (legacy_plugin_dir / "compound-learning").symlink_to(tmp_path / "missing-plugin")

    ready = bootstrap.dependency_ready(bootstrap.EMBEDDING)

    assert isinstance(ready, bool)
    assert bootstrap.managed_site_dir() == tmp_path / ".claude" / "compound-learning" / "site-packages"


def test_module_discovery_includes_legacy_site_packages(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", list(sys.path))

    legacy_site_dir = tmp_path / ".claude" / "plugins" / "compound-learning" / "site-packages"
    legacy_site_dir.mkdir(parents=True)
    (legacy_site_dir / "legacy_bootstrap_probe_module.py").write_text(
        "value = 'legacy'\n",
        encoding="utf-8",
    )

    assert bootstrap._module_available("legacy_bootstrap_probe_module") is True


def test_probe_dependency_returns_ready_for_persisted_ready_state(monkeypatch):
    ready = {
        bootstrap.CORE: True,
        bootstrap.EMBEDDING: False,
    }
    install_calls = []

    monkeypatch.setattr(bootstrap, "dependency_ready", lambda dependency: ready[dependency])
    monkeypatch.setattr(
        bootstrap,
        "_install_packages",
        lambda dependency, packages: install_calls.append((dependency, packages)) or "fake-installer",
    )

    persisted_status = {
        "dependencies": {
            bootstrap.CORE: {
                "state": bootstrap.STATE_READY,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:00+00:00",
                "pid": None,
                "error": None,
            },
            bootstrap.EMBEDDING: {
                "state": bootstrap.STATE_MISSING,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "started_at": None,
                "finished_at": None,
                "pid": None,
                "error": None,
            },
        }
    }
    bootstrap.state_dir()
    bootstrap.status_file().write_text(json.dumps(persisted_status), encoding="utf-8")

    result = bootstrap.probe_dependency(bootstrap.CORE)

    assert result.state == bootstrap.STATE_READY
    assert result.changed is False
    assert install_calls == []


def test_probe_dependency_ready_state_falls_back_when_runtime_is_missing(monkeypatch):
    ready = {
        bootstrap.CORE: False,
        bootstrap.EMBEDDING: False,
    }

    monkeypatch.setattr(bootstrap, "dependency_ready", lambda dependency: ready[dependency])

    persisted_status = {
        "dependencies": {
            bootstrap.CORE: {
                "state": bootstrap.STATE_READY,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:00+00:00",
                "pid": None,
                "error": None,
            },
            bootstrap.EMBEDDING: {
                "state": bootstrap.STATE_MISSING,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "started_at": None,
                "finished_at": None,
                "pid": None,
                "error": None,
            },
        }
    }
    bootstrap.state_dir()
    bootstrap.status_file().write_text(json.dumps(persisted_status), encoding="utf-8")

    result = bootstrap.probe_dependency(bootstrap.CORE)

    assert result.state == bootstrap.STATE_MISSING
    assert result.changed is True

    status = bootstrap.read_status()
    assert status["dependencies"][bootstrap.CORE]["state"] == bootstrap.STATE_MISSING


def test_probe_dependency_normalizes_stale_install_to_failed(monkeypatch):
    ready = {
        bootstrap.CORE: False,
        bootstrap.EMBEDDING: False,
    }

    monkeypatch.setattr(bootstrap, "dependency_ready", lambda dependency: ready[dependency])
    monkeypatch.setattr(bootstrap, "_pid_is_running", lambda pid: False)

    stale_status = {
        "dependencies": {
            bootstrap.CORE: {
                "state": bootstrap.STATE_INSTALLING,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": None,
                "pid": 424242,
                "error": None,
            },
            bootstrap.EMBEDDING: {
                "state": bootstrap.STATE_MISSING,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "started_at": None,
                "finished_at": None,
                "pid": None,
                "error": None,
            },
        }
    }
    bootstrap.state_dir()
    bootstrap.status_file().write_text(json.dumps(stale_status), encoding="utf-8")

    result = bootstrap.probe_dependency(bootstrap.CORE)

    assert result.state == bootstrap.STATE_FAILED
    assert result.error is not None
    assert "exited before the runtime became available" in result.error

    status = bootstrap.read_status()
    assert status["dependencies"][bootstrap.CORE]["state"] == bootstrap.STATE_FAILED


@pytest.mark.parametrize("corrupt_contents", [None, "{not-json"])
def test_missing_or_corrupt_status_files_recover_safely(monkeypatch, corrupt_contents):
    ready = {
        bootstrap.CORE: False,
        bootstrap.EMBEDDING: False,
    }
    install_calls = []

    monkeypatch.setattr(bootstrap, "dependency_ready", lambda dependency: ready[dependency])
    monkeypatch.setattr(
        bootstrap,
        "missing_packages",
        lambda dependency: [] if ready[dependency] else ["sqlite-vec"],
    )

    def fake_install(dependency, packages):
        install_calls.append((dependency, packages))
        ready[dependency] = True
        return "fake-installer"

    monkeypatch.setattr(bootstrap, "_install_packages", fake_install)

    bootstrap.state_dir()
    if corrupt_contents is not None:
        bootstrap.status_file().write_text(corrupt_contents, encoding="utf-8")

    probe = bootstrap.probe_dependency(bootstrap.CORE)
    assert probe.state == bootstrap.STATE_MISSING

    result = bootstrap.ensure_core_dependencies()
    assert result.state == bootstrap.STATE_READY
    assert install_calls == [(bootstrap.CORE, ["sqlite-vec"])]

    status = bootstrap.read_status()
    assert status["dependencies"][bootstrap.CORE]["state"] == bootstrap.STATE_READY


def test_stale_install_state_becomes_failed_and_can_recover(monkeypatch):
    ready = {
        bootstrap.CORE: True,
        bootstrap.EMBEDDING: False,
    }

    monkeypatch.setattr(bootstrap, "dependency_ready", lambda dependency: ready[dependency])
    monkeypatch.setattr(bootstrap, "_pid_is_running", lambda pid: False)

    stale_status = {
        "dependencies": {
            bootstrap.CORE: {
                "state": bootstrap.STATE_READY,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "started_at": None,
                "finished_at": None,
                "pid": None,
                "error": None,
            },
            bootstrap.EMBEDDING: {
                "state": bootstrap.STATE_INSTALLING,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": None,
                "pid": 424242,
                "error": None,
            },
        }
    }
    bootstrap.state_dir()
    bootstrap.status_file().write_text(json.dumps(stale_status), encoding="utf-8")

    status = bootstrap.read_status()
    failed_state = status["dependencies"][bootstrap.EMBEDDING]
    assert failed_state["state"] == bootstrap.STATE_FAILED
    assert "exited before the runtime became available" in failed_state["error"]

    monkeypatch.setattr(
        bootstrap,
        "missing_packages",
        lambda dependency: [] if ready[dependency] else ["sentence-transformers"],
    )

    def fake_install(dependency, packages):
        assert dependency == bootstrap.EMBEDDING
        assert packages == ["sentence-transformers"]
        ready[dependency] = True
        return "fake-installer"

    monkeypatch.setattr(bootstrap, "_install_packages", fake_install)

    result = bootstrap.ensure_embedding_dependencies()
    assert result.state == bootstrap.STATE_READY
    assert result.backend == "fake-installer"

    recovered_status = bootstrap.read_status()
    assert recovered_status["dependencies"][bootstrap.EMBEDDING]["state"] == bootstrap.STATE_READY


def test_missing_installer_reports_embedding_packages(monkeypatch):
    monkeypatch.setattr(bootstrap, "_python_has_pip", lambda: False)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda binary: None)
    monkeypatch.setattr(
        bootstrap,
        "missing_packages",
        lambda dependency: ["sentence-transformers"]
        if dependency == bootstrap.EMBEDDING
        else ["pysqlite3-binary", "sqlite-vec"],
    )

    with pytest.raises(bootstrap.BootstrapError) as excinfo:
        bootstrap._build_install_command(bootstrap.EMBEDDING, ["sentence-transformers"])

    message = str(excinfo.value)
    assert "sentence-transformers" in message
    assert "sqlite-vec" not in message


def test_prepare_embedding_for_auto_peek_is_non_blocking(monkeypatch):
    ready = {
        bootstrap.CORE: True,
        bootstrap.EMBEDDING: False,
    }
    spawn_calls = []

    monkeypatch.setattr(bootstrap, "dependency_ready", lambda dependency: ready[dependency])
    monkeypatch.setattr(
        bootstrap,
        "missing_packages",
        lambda dependency: [] if ready[dependency] else ["sentence-transformers"],
    )
    monkeypatch.setattr(
        bootstrap,
        "_spawn_background_install",
        lambda dependency: spawn_calls.append(dependency) or 98765,
    )
    monkeypatch.setattr(bootstrap, "_pid_is_running", lambda pid: pid == 98765)

    first = bootstrap.prepare_embedding_for_auto_peek()
    second = bootstrap.prepare_embedding_for_auto_peek()

    assert first.state == "started"
    assert first.pid == 98765
    assert second.state == bootstrap.STATE_INSTALLING
    assert second.pid == 98765
    assert spawn_calls == [bootstrap.EMBEDDING]

    status = bootstrap.read_status()
    embedding_state = status["dependencies"][bootstrap.EMBEDDING]
    assert embedding_state["state"] == bootstrap.STATE_INSTALLING
    assert embedding_state["pid"] == 98765
