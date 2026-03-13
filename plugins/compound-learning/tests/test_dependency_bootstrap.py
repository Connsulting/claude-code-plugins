import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

BOOTSTRAP_PATH = Path(__file__).resolve().parent.parent / "lib" / "bootstrap.py"


@pytest.fixture
def load_bootstrap(monkeypatch):
    loaded_modules = []

    def _load(home: Path):
        monkeypatch.setenv("HOME", str(home))
        module_name = f"compound_learning_bootstrap_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, BOOTSTRAP_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module._python_has_pip.cache_clear()
        loaded_modules.append((module_name, module))
        return module

    yield _load

    for module_name, module in loaded_modules:
        module._python_has_pip.cache_clear()
        sys.modules.pop(module_name, None)


def test_import_isolation_avoids_runtime_side_effects(load_bootstrap, tmp_path):
    bootstrap = load_bootstrap(tmp_path)

    assert bootstrap.state_dir() == tmp_path / ".claude" / "state" / "compound-learning"
    assert not bootstrap.state_dir().exists()
    assert not bootstrap.log_file().exists()
    assert str(bootstrap.managed_site_dir()) not in sys.path


@pytest.mark.parametrize("kind", ["broken_symlink", "file"])
def test_legacy_runtime_path_conflicts_are_reported_without_crashing(
    kind,
    load_bootstrap,
    tmp_path,
):
    legacy_path = tmp_path / ".claude" / "plugins" / "compound-learning"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "broken_symlink":
        legacy_path.symlink_to(tmp_path / "missing-plugin-root")
    else:
        legacy_path.write_text("not a directory", encoding="utf-8")

    bootstrap = load_bootstrap(tmp_path)

    status = bootstrap.read_status()
    warning = status["legacy_path_warning"]

    assert str(legacy_path) in warning
    assert str(bootstrap.state_dir()) in warning
    assert str(bootstrap.managed_site_dir()) in warning
    assert bootstrap.status_file().exists()


def test_missing_packages_distinguishes_core_and_embedding(load_bootstrap, tmp_path, monkeypatch):
    bootstrap = load_bootstrap(tmp_path)
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


def test_stale_install_state_becomes_failed_and_can_recover(load_bootstrap, tmp_path, monkeypatch):
    bootstrap = load_bootstrap(tmp_path)
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
    bootstrap.status_file().parent.mkdir(parents=True, exist_ok=True)
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


def test_embedding_no_installer_message_avoids_pip_guidance_and_uses_embedding_packages(
    load_bootstrap,
    tmp_path,
    monkeypatch,
):
    bootstrap = load_bootstrap(tmp_path)

    monkeypatch.setattr(bootstrap, "dependency_ready", lambda dependency: False)
    monkeypatch.setattr(
        bootstrap,
        "missing_packages",
        lambda dependency: ["sentence-transformers"] if dependency == bootstrap.EMBEDDING else [],
    )

    def fake_python_has_pip():
        return False

    fake_python_has_pip.cache_clear = lambda: None

    monkeypatch.setattr(bootstrap, "_python_has_pip", fake_python_has_pip)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    with pytest.raises(bootstrap.BootstrapError) as exc_info:
        bootstrap.ensure_embedding_dependencies()

    message = str(exc_info.value)
    assert "sentence-transformers" in message
    assert "sqlite-vec" not in message
    assert "pysqlite3" not in message
    assert "pip install --target" not in message
    assert "using a Python environment with pip or uv available" in message
    assert str(bootstrap.managed_site_dir()) in message


def test_prepare_embedding_for_auto_peek_is_non_blocking(load_bootstrap, tmp_path, monkeypatch):
    bootstrap = load_bootstrap(tmp_path)
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
