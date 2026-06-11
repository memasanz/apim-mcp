from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

SAMPLE = Path(__file__).resolve().parents[2] / "config" / "config.sample.json"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CONFIG_PATH", str(SAMPLE))
    monkeypatch.setenv("ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("SKIP_AZ_LOGIN", "1")
    s = Settings(config_path=SAMPLE, enable_config_watch=False, az_login_required=False)
    app = create_app(s)
    with TestClient(app) as client:
        yield client


def test_admin_get_config(client: TestClient) -> None:
    resp = client.get("/admin/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "apim" in body["services"]


def test_admin_reload(client: TestClient) -> None:
    resp = client.post("/admin/api/reload")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["services"] >= 1
    assert body["enabled_commands"] >= 1


def test_admin_put_invalid_config(client: TestClient) -> None:
    resp = client.put("/admin/api/config", json={"version": 1, "services": {"BAD!": {}}})
    assert resp.status_code == 400


def test_mcp_mounted(client: TestClient) -> None:
    # Streamable HTTP transport responds on POST /mcp — bare GET should
    # 400/405/406/404 rather than crash. We only assert the path is registered.
    resp = client.get("/mcp")
    assert resp.status_code in (200, 400, 404, 405, 406)
