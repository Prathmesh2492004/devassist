from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_db

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    database: str | None = None


@router.get("/health")
def health() -> HealthResponse:
    """Liveness: the process is up. Never touches the database."""
    return HealthResponse(status="ok")


@router.get("/health/ready")
def ready(response: Response, db: Annotated[Session, Depends(get_db)]) -> HealthResponse:
    """Readiness: the API can reach Postgres. Returns 503 if it cannot."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", database="unreachable")
    return HealthResponse(status="ok", database="ok")
