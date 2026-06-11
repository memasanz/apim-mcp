
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import Principal, require_admin, require_principal


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()

    @a.get("/me")
    async def me(p: Principal = Depends(require_principal)) -> dict:
        return {"subject": p.subject, "roles": list(p.roles)}

    @a.get("/admin-only")
    async def admin_only(p: Principal = Depends(require_admin)) -> dict:
        return {"subject": p.subject}

    return a


def test_api_key_accepted(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "secret")
    monkeypatch.delenv("ALLOW_ANONYMOUS", raising=False)
    client = TestClient(app)
    resp = client.get("/me", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
    assert resp.json()["subject"] == "api-key"


def test_api_key_rejected(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "secret")
    monkeypatch.delenv("ALLOW_ANONYMOUS", raising=False)
    client = TestClient(app)
    resp = client.get("/me", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


def test_missing_creds_rejected(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("MCP_API_KEY", "ALLOW_ANONYMOUS"):
        monkeypatch.delenv(k, raising=False)
    client = TestClient(app)
    resp = client.get("/me")
    assert resp.status_code == 401


def test_anonymous_mode(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_ANONYMOUS", "true")
    client = TestClient(app)
    resp = client.get("/admin-only")
    assert resp.status_code == 200
    assert resp.json()["subject"] == "anonymous"


def test_api_key_lacks_admin_role(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "secret")
    monkeypatch.delenv("ALLOW_ANONYMOUS", raising=False)
    client = TestClient(app)
    resp = client.get("/admin-only", headers={"X-API-Key": "secret"})
    assert resp.status_code == 403
