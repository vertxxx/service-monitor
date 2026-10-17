import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request as URLRequest, urlopen

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import CheckResult, Service

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import CONTENT_TYPE_LATEST
from fastapi import Request, Response
import logging

from apscheduler.schedulers.background import BackgroundScheduler


logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

app = FastAPI(
    title="Service Monitor",
    description="HTTP/HTTPS service monitoring API",
    version="0.4.0",
)

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    ["method", "endpoint", "status"],
)

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request processing time in seconds",
    ["method", "endpoint"],
)

# метрики для мониторинга сервисов

SERVICE_UP = Gauge(
    "monitored_service_up",
    "Whether the monitored service is available (1 = up, 0 = down)",
    ["service_id", "service_name"],
)

SERVICE_RESPONSE_TIME = Gauge(
    "monitored_service_response_time_seconds",
    "Response time of the last service check in seconds",
    ["service_id", "service_name"],
)

SERVICE_HTTP_STATUS = Gauge(
    "monitored_service_http_status_code",
    "HTTP status code returned by the last check; 0 if unavailable",
    ["service_id", "service_name"],
)

SERVICE_CHECKS_TOTAL = Counter(
    "monitored_service_checks",
    "Total number of checks performed on monitored services",
    ["service_id", "service_name", "status"],
)


@app.middleware("http")
async def collect_http_metrics(request: Request, call_next):
    started_at = time.perf_counter()

    response = await call_next(request)

    route = request.scope.get("route")
    endpoint = route.path if route else "unmatched"

    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=endpoint,
        status=str(response.status_code),
    ).inc()

    REQUEST_DURATION.labels(
        method=request.method,
        endpoint=endpoint,
    ).observe(time.perf_counter() - started_at)

    return response


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

    request = URLRequest(
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


def run_scheduled_checks():
    """Check all registered services every five minutes."""
    logger.info("Starting scheduled service checks")

    db = SessionLocal()

    try:
        # сохранение данных сервисов в список
        services = db.scalars(
            select(Service).order_by(Service.id)
        ).all()

        services_data = [
            (service.id, service.name, service.url)
            for service in services
        ]

        for service_id, service_name, service_url in services_data:
            try:
                result = check_url(service_url)

                labels = {
                    "service_id": str(service_id),
                    "service_name": service_name,
                }

                # обновляет метрики prometheus
                SERVICE_UP.labels(**labels).set(
                    1 if result["status"] == "OK" else 0
                )

                SERVICE_RESPONSE_TIME.labels(**labels).set(
                    (result["response_time_ms"] or 0) / 1000
                )

                SERVICE_HTTP_STATUS.labels(**labels).set(
                    result["http_status"] or 0
                )

                SERVICE_CHECKS_TOTAL.labels(
                    **labels,
                    status=result["status"],
                ).inc()

                # сохраняем результат проверки в sql
                db.add(
                    CheckResult(
                        service_id=service_id,
                        **result,
                    )
                )
                db.commit()

                logger.info(
                    "Checked %s: %s",
                    service_name,
                    result["status"],
                )

            except Exception:
                db.rollback()
                logger.exception(
                    "Failed to process check for service %s",
                    service_name,
                )

    finally:
        db.close()

    logger.info("Scheduled service checks completed")


scheduler.add_job(
    run_scheduled_checks,
    trigger="interval",
    seconds=60,
    id="service_checks",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
)


@app.on_event("startup")
def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started: checks every 60 seconds")


@app.on_event("shutdown")
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
        

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
    
    # инфо о метриках при каждом чеке
    labels = {
        "service_id": str(service.id),
        "service_name": service.name,
    }

    SERVICE_UP.labels(**labels).set(
        1 if result["status"] == "OK" else 0
    )

    SERVICE_RESPONSE_TIME.labels(**labels).set(
        result["response_time_ms"] / 1000
    )

    SERVICE_HTTP_STATUS.labels(**labels).set(
        result["http_status"] or 0
    )

    SERVICE_CHECKS_TOTAL.labels(
        **labels,
        status=result["status"],
    ).inc()

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


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
