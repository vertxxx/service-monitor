import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app


# Separate in-memory database for tests.
test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    autocommit=False,
)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield


def test_health_check():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_and_list_service():
    response = client.post(
        "/services",
        json={
            "name": "Example",
            "url": "https://example.com",
        },
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Example"
    assert response.json()["url"] == "https://example.com"

    list_response = client.get("/services")

    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert list_response.json()[0]["name"] == "Example"


def test_reject_invalid_url():
    response = client.post(
        "/services",
        json={
            "name": "Invalid",
            "url": "not-a-url",
        },
    )

    assert response.status_code == 422


def test_reject_empty_name():
    response = client.post(
        "/services",
        json={
            "name": "   ",
            "url": "https://example.com",
        },
    )

    assert response.status_code == 422


def test_get_missing_service():
    response = client.get("/services/999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Service not found"


def test_check_service_success(monkeypatch):
    create_response = client.post(
        "/services",
        json={
            "name": "Example",
            "url": "https://example.com",
        },
    )
    service_id = create_response.json()["id"]

    def fake_check_url(url):
        return {
            "status": "OK",
            "http_status": 200,
            "response_time_ms": 123.45,
            "error": None,
        }

    monkeypatch.setattr("app.main.check_url", fake_check_url)

    response = client.post(f"/services/{service_id}/check")

    assert response.status_code == 200
    assert response.json()["status"] == "OK"
    assert response.json()["http_status"] == 200
    assert response.json()["response_time_ms"] == 123.45

    history_response = client.get(
        f"/services/{service_id}/checks"
    )

    assert history_response.status_code == 200
    assert len(history_response.json()) == 1
    assert history_response.json()[0]["status"] == "OK"


def test_check_service_failure(monkeypatch):
    create_response = client.post(
        "/services",
        json={
            "name": "Unavailable",
            "url": "https://example.com",
        },
    )
    service_id = create_response.json()["id"]

    def fake_check_url(url):
        return {
            "status": "FAIL",
            "http_status": None,
            "response_time_ms": 5000.0,
            "error": "Connection timed out",
        }

    monkeypatch.setattr("app.main.check_url", fake_check_url)

    response = client.post(f"/services/{service_id}/check")

    assert response.status_code == 200
    assert response.json()["status"] == "FAIL"
    assert response.json()["error"] == "Connection timed out"

    history_response = client.get(
        f"/services/{service_id}/checks"
    )

    assert len(history_response.json()) == 1
    assert history_response.json()[0]["status"] == "FAIL"


def test_check_missing_service():
    response = client.post("/services/999/check")

    assert response.status_code == 404


def test_check_history_for_missing_service():
    response = client.get("/services/999/checks")

    assert response.status_code == 404
