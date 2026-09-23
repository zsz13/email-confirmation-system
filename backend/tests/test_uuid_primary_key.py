"""Submissions are keyed by a UUIDv7 stored as sixteen raw bytes."""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateTable

from app.db.base import Base
from app.db.types import UUIDBinary
from app.models.submission import Submission

MYSQL = mysql.dialect()


def test_uuid_is_stored_as_sixteen_bytes_and_loaded_back() -> None:
    value = uuid.uuid7()

    stored = UUIDBinary().process_bind_param(value, MYSQL)

    assert stored == value.bytes
    assert stored is not None and len(stored) == 16
    assert UUIDBinary().process_result_value(stored, MYSQL) == value
    assert UUIDBinary().process_result_value(None, MYSQL) is None


def test_python_type_lets_the_admin_parse_canonical_strings() -> None:
    assert UUIDBinary().python_type is uuid.UUID


def test_mysql_ddl_uses_a_binary_16_primary_key_and_nothing_else() -> None:
    ddl = str(CreateTable(Base.metadata.tables["submissions"]).compile(dialect=MYSQL))

    assert "id BINARY(16) NOT NULL" in ddl
    assert "PRIMARY KEY (id)" in ddl
    assert "AUTO_INCREMENT" not in ddl
    assert not re.search(r"^\s+id BIGINT", ddl, re.MULTILINE)


async def test_new_rows_get_time_ordered_version_7_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        for index in range(5):
            session.add(Submission(name=f"n{index}", email=f"u{index}@example.com"))
        await session.commit()
        ids = (
            (await session.execute(select(Submission.id).order_by(Submission.name)))
            .scalars()
            .all()
        )

    assert all(isinstance(value, uuid.UUID) for value in ids)
    assert {value.version for value in ids} == {7}
    assert len(set(ids)) == 5
    # UUIDv7 leads with the timestamp, so insertion order and key order agree.
    assert ids == sorted(ids)


async def test_rows_are_found_by_their_uuid(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        submission = Submission(name="Daniyar", email="d@example.com")
        session.add(submission)
        await session.commit()
        key = submission.id

    async with session_factory() as session:
        found = await session.get(Submission, key)
    assert found is not None
    assert found.email == "d@example.com"
