from fastapi.testclient import TestClient

from app.main import app


def test_app_imports() -> None:
    assert app is not None


def test_healthcheck() -> None:
    client = TestClient(app)
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_html_routes() -> None:
    client = TestClient(app)

    for route in ["/", "/history", "/settings"]:
        response = client.get(route)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
