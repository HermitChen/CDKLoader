from __future__ import annotations

from datetime import datetime, timedelta, timezone


# Persisted timestamps are UTC-naive for compatibility with the existing
# SQLite schema. Convert only at the boundaries where administrators inspect
# times or files are produced for them.
UTC = timezone.utc
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_utc_naive(value: datetime) -> datetime:
    return as_utc(value).replace(tzinfo=None)


def to_china_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return as_utc(value).astimezone(CHINA_TIMEZONE).isoformat()


def china_now() -> datetime:
    return datetime.now(UTC).astimezone(CHINA_TIMEZONE)


def china_day_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    local_now = as_utc(now or datetime.now(UTC)).astimezone(CHINA_TIMEZONE)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(UTC).replace(tzinfo=None),
        local_end.astimezone(UTC).replace(tzinfo=None),
    )
