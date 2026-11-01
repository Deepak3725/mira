"""Retention helpers for append-only operational tables.

Only allowlisted tables may be purged, and only by age. Product tables
(users, twins, conversations, ...) are never touched here.
"""

from __future__ import annotations

import datetime as dt

PURGEABLE_TABLES = ("usage_log", "audit_events")


def purge_old_rows(conn, table: str, days: int, *,
                   now: dt.datetime | None = None) -> int:
    """Delete rows older than `days` from an allowlisted table.

    Returns the number of rows removed. Commits immediately.
    """
    if table not in PURGEABLE_TABLES:
        raise ValueError(f"Refusing to purge non-operational table {table!r}.")
    moment = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (moment - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount
