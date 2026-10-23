import pytest


@pytest.fixture(autouse=True)
def isolate_push_configuration(monkeypatch):
    monkeypatch.delenv('JPUSH_APP_KEY', raising=False)
    monkeypatch.delenv('JPUSH_MASTER_SECRET', raising=False)
