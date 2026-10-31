from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import get_db
from app.main import create_app


def test_health_is_ok(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "database": None}


def test_ready_when_database_reachable(client: TestClient) -> None:
    res = client.get("/health/ready")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "database": "ok"}


def test_ready_returns_503_when_database_down() -> None:
    broken = MagicMock()
    broken.execute.side_effect = OperationalError("SELECT 1", {}, Exception("down"))
    app = create_app()
    app.dependency_overrides[get_db] = lambda: broken

    res = TestClient(app).get("/health/ready")

    assert res.status_code == 503
    assert res.json() == {"status": "degraded", "database": "unreachable"}
