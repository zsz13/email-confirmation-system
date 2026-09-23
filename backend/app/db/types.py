import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BINARY, DateTime
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine


class UTCDateTime(TypeDecorator[datetime]):
    """Persist timezone-aware datetimes as naive UTC and load them back as aware UTC.

    MySQL ``DATETIME`` stores no offset, so the application normalises to UTC on the way
    in and re-attaches the UTC zone on the way out. This keeps every datetime the
    application handles timezone-aware.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        # MySQL DATETIME defaults to whole seconds; keep microseconds so rows created within
        # the same second stay ordered and read back exactly as they were stored.
        if dialect.name == "mysql":
            return dialect.type_descriptor(MYSQL_DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,  # noqa: ARG002  # part of the TypeDecorator interface
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime only accepts timezone-aware datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,  # noqa: ARG002  # part of the TypeDecorator interface
    ) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class UUIDBinary(TypeDecorator[uuid.UUID]):
    """A ``uuid.UUID`` stored as 16 raw bytes (``BINARY(16)``).

    MySQL has no native UUID column type. Sixteen bytes take less than half the space of the
    36-character text form and, because UUIDv7 starts with a timestamp, the primary key
    index stays append-ordered. Python code only ever sees ``uuid.UUID`` objects; the raw
    bytes never leave the database layer.
    """

    impl = BINARY(16)
    cache_ok = True

    @property
    def python_type(self) -> type[uuid.UUID]:
        return uuid.UUID

    def process_bind_param(
        self,
        value: uuid.UUID | None,
        dialect: Dialect,  # noqa: ARG002  # part of the TypeDecorator interface
    ) -> bytes | None:
        return None if value is None else value.bytes

    def process_result_value(
        self,
        value: bytes | None,
        dialect: Dialect,  # noqa: ARG002  # part of the TypeDecorator interface
    ) -> uuid.UUID | None:
        if value is None:
            return None
        return uuid.UUID(bytes=value)
