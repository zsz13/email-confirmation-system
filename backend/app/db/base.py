from datetime import datetime

from sqlalchemy.orm import DeclarativeBase

from app.db.types import UTCDateTime


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""

    # Every ``Mapped[datetime]`` column is stored as UTC and loaded timezone-aware.
    type_annotation_map = {datetime: UTCDateTime}  # noqa: RUF012  # SQLAlchemy class-level hook
