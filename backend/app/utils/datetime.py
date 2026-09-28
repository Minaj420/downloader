from datetime import UTC, datetime


def utc_isoformat(value: datetime | str) -> str:
    """Return an aware ISO timestamp in UTC; SQLite-naive values are UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
