from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import sqlite

from app.db.types import UTCDateTime

DIALECT = sqlite.dialect()


def test_aware_datetime_is_stored_as_naive_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    value = datetime(2026, 9, 21, 12, 30, tzinfo=plus_two)

    assert UTCDateTime().process_bind_param(value, DIALECT) == datetime(
        2026, 9, 21, 10, 30
    )


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        UTCDateTime().process_bind_param(datetime(2026, 9, 21, 12, 30), DIALECT)


def test_loaded_value_is_aware_utc() -> None:
    loaded = UTCDateTime().process_result_value(datetime(2026, 9, 21, 10, 30), DIALECT)

    assert loaded == datetime(2026, 9, 21, 10, 30, tzinfo=UTC)
    assert UTCDateTime().process_result_value(None, DIALECT) is None
