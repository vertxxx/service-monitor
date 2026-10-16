import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CheckResult, Service


app = FastAPI(
    title="Service Monitor",
    description="HTTP/HTTPS service monitoring API",
    version="0.4.0",
)


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Service name cannot be empty")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)

        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or any(char.isspace() for char in value)
        ):
            raise ValueError("URL must be a valid HTTP or HTTPS URL")

        return value


def check_url(url: str, timeout: float = 5.0) -> dict:
    """Check an HTTP/HTTPS URL and measure its response time."""
    result = {
        "status": "FAIL",
        "http_status": None,
        "response_time_ms": None,
        "error": None,
    }

    request = Request(
        url,
        headers={"User-Agent": "ServiceMonitor/1.0"},
        method="GET",
    )

    started_at = time.perf_counter()

    try:
        with urlopen(request, timeout=timeout) as response:
            result["http_status"] = response.status
            result["status"] = (
                "OK" if 200 <= response.status < 400 else "FAIL"
            )

    except HTTPError as error:
        result["http_status"] = error.code
        result["status"] = (
            "OK" if 200 <= error.code < 400 else "FAIL"
        )
        result["error"] = f"HTTP error: {error.code}"

    except (URLError, TimeoutError, OSError) as error:
        result["error"] = str(error)

    finally:
        result["response_time_ms"] = round(
            (time.perf_counter() - started_at) * 1000,
            2,
        )

    return result


def service_to_dict(service: Service) -> dict:
    return {
        "id": service.id,
        "name": service.name,
        "url": service.url,
        "created_at": service.created_at,
    }


def check_to_dict(result: CheckResult) -> dict:
    return {
        "id": result.id,
        "service_id": result.service_id,
        "status": result.status,
        "http_status": result.http_status,
        "response_time_ms": result.response_time_ms,
        "error": result.error,
        "checked_at": result.checked_at,
    }


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/services", status_code=201)
def create_service(
    service: ServiceCreate,
    db: Session = Depends(get_db),
):
    new_service = Service(
        name=service.name,
        url=service.url,
    )

    db.add(new_service)
    db.commit()
    db.refresh(new_service)

    return service_to_dict(new_service)


@app.get("/services")
def list_services(db: Session = Depends(get_db)):
    services = db.scalars(
        select(Service).order_by(Service.id)
    ).all()

    return [service_to_dict(service) for service in services]


@app.get("/services/{service_id}")
def get_service(
    service_id: int,
    db: Session = Depends(get_db),
):
    service = db.get(Service, service_id)

    if service is None:
        raise HTTPException(
            status_code=404,
            detail="Service not found",
        )

    return service_to_dict(service)


@app.post("/services/{service_id}/check")
def check_service(
    service_id: int,
    db: Session = Depends(get_db),
):
    service = db.get(Service, service_id)

    if service is None:
        raise HTTPException(
            status_code=404,
            detail="Service not found",
        )

    result = check_url(service.url)

    check_record = CheckResult(
        service_id=service.id,
        **result,
    )

    db.add(check_record)
    db.commit()
    db.refresh(check_record)

    return {
        **check_to_dict(check_record),
        "service_name": service.name,
        "url": service.url,
    }


@app.get("/services/{service_id}/checks")
def list_service_checks(
    service_id: int,
    db: Session = Depends(get_db),
):
    service = db.get(Service, service_id)

    if service is None:
        raise HTTPException(
            status_code=404,
            detail="Service not found",
        )

    results = db.scalars(
        select(CheckResult)
        .where(CheckResult.service_id == service_id)
        .order_by(CheckResult.checked_at.desc(), CheckResult.id.desc())
    ).all()

    return [check_to_dict(result) for result in results]
