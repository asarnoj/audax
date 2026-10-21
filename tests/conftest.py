import pytest

import agent.config as config


@pytest.fixture(autouse=True)
def isolated_instance(tmp_path_factory, monkeypatch):
    """Every test gets its own instance built from the engine's examples, never the real one."""
    inst = tmp_path_factory.mktemp("instance")
    monkeypatch.setattr(config, "INSTANCE", inst)
    config.ensure_instance()
    return inst


@pytest.fixture(autouse=True)
def fresh_routines(monkeypatch):
    """The routines' in-memory copy (agent/routines.py) starts empty and unloaded in every test."""
    from agent import routines

    monkeypatch.setattr(routines, "_streams", [])
    monkeypatch.setattr(routines, "_missions", [])
    monkeypatch.setattr(routines, "_loaded", False)
    monkeypatch.setattr(routines, "on_streams_changed", [])
