"""冒烟测试：确认 FastAPI app 能启动且健康检查通过。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cg.main import app
from cg.settings import Settings, get_settings


def test_health() -> None:
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_root() -> None:
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "CompeteGraph API"
        assert "version" in body


def test_runs_require_login() -> None:
    with TestClient(app) as client:
        r = client.get("/api/runs")
        assert r.status_code == 401


def test_login_allows_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CG_AUTH_USERNAME", "test-user")
    monkeypatch.setenv("CG_AUTH_PASSWORD", "test-password")
    monkeypatch.setenv("CG_AUTH_SECRET", "test-session-secret")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            r = client.post("/api/login", json={"username": "test-user", "password": "test-password"})
            assert r.status_code == 200
            assert r.json()["authenticated"] is True

            runs = client.get("/api/runs")
            assert runs.status_code == 200
    finally:
        get_settings.cache_clear()


def test_ark_llm_settings_select_endpoint_and_key() -> None:
    settings = Settings(
        cg_llm_provider="ark",
        cg_llm_model="ep-example",
        ark_api_key="ark-test-key",
    )

    assert settings.active_llm_api_key == "ark-test-key"
    assert settings.active_llm_base_url == "https://ark.cn-beijing.volces.com/api/v3"
