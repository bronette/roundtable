import pytest

from roundtable.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def run(store):
    pid = store.create_project(name="t", objective="o", requirements=["r1"], config={})
    return store.create_run(project_id=pid, workspace_path=str("/tmp/x"))


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    import roundtable.agents as agents
    monkeypatch.setattr(agents, "_sleep", lambda s: None)
