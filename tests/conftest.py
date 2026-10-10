import pytest

import agent.config as config


@pytest.fixture(autouse=True)
def isolated_instance(tmp_path_factory, monkeypatch):
    """Every test gets its own instance built from the engine's examples, never the real one."""
    inst = tmp_path_factory.mktemp("instance")
    monkeypatch.setattr(config, "INSTANCE", inst)
    config.ensure_instance()
    return inst
