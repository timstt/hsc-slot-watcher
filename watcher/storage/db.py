from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

from watcher.models import Slot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot(
    center_id INTEGER NOT NULL, day TEXT NOT NULL, start TEXT NOT NULL,
    PRIMARY KEY(center_id, day, start));
CREATE TABLE IF NOT EXISTS days(
    center_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY(center_id, day));
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS appearances(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    center_id INTEGER NOT NULL, day TEXT NOT NULL, start TEXT NOT NULL,
    seen_at TEXT NOT NULL);
"""


class Database:
    """SQLite storage. A single connection guarded by a mutex (monitor + bot thread)."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- kv ---
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def kv_del(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM kv WHERE key=?", (key,))

    # --- snapshot ---
    def get_snapshot(self, center_id: int) -> set[Slot] | None:
        """None -- there has not been a single successful cycle for this center yet."""
        if self.kv_get(f"init:{center_id}") is None:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, start FROM snapshot WHERE center_id=?", (center_id,)
            ).fetchall()
        return {Slot(center_id, date.fromisoformat(d), time.fromisoformat(t)) for d, t in rows}

    def replace_snapshot(self, center_id: int, slots: set[Slot]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM snapshot WHERE center_id=?", (center_id,))
            self._conn.executemany(
                "INSERT INTO snapshot(center_id, day, start) VALUES(?,?,?)",
                [(center_id, s.day.isoformat(), s.start.isoformat()) for s in slots],
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (f"init:{center_id}", "1")
            )

    def known_days(self, center_id: int) -> set[date]:
        """Dates that were read at least once and are still in the e-queue."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT day FROM days WHERE center_id=?", (center_id,)
            ).fetchall()
        return {date.fromisoformat(r[0]) for r in rows}

    def apply_partial(
        self, center_id: int, covered: set[date], slots: set[Slot], valid_days: set[date]
    ) -> None:
        """Update the snapshot only for the read dates; drop dates no longer on the site."""
        keep = {d.isoformat() for d in valid_days}
        with self._lock, self._conn:
            for table in ("snapshot", "days"):
                stored = {r[0] for r in self._conn.execute(
                    f"SELECT DISTINCT day FROM {table} WHERE center_id=?", (center_id,))}
                gone = (stored - keep) | ({d.isoformat() for d in covered} if table == "snapshot" else set())
                self._conn.executemany(
                    f"DELETE FROM {table} WHERE center_id=? AND day=?",
                    [(center_id, d) for d in gone],
                )
            self._conn.executemany(
                "INSERT INTO snapshot(center_id, day, start) VALUES(?,?,?)",
                [(center_id, s.day.isoformat(), s.start.isoformat()) for s in slots],
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO days(center_id, day) VALUES(?,?)",
                [(center_id, d.isoformat()) for d in covered],
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (f"init:{center_id}", "1")
            )

    # --- service messages: no more than once every N hours ---
    def service_due(self, key: str, now: datetime, hours: float) -> bool:
        last = self.kv_get(f"svc:{key}")
        return last is None or now - datetime.fromisoformat(last) >= timedelta(hours=hours)

    def service_mark(self, key: str, now: datetime) -> None:
        self.kv_set(f"svc:{key}", now.isoformat())

    # --- statistics ---
    def record_appearances(self, slots: list[Slot], seen_at: datetime) -> None:
        stamp = seen_at.replace(tzinfo=None).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO appearances(center_id, day, start, seen_at) VALUES(?,?,?,?)",
                [(s.center_id, s.day.isoformat(), s.start.isoformat(), stamp) for s in slots],
            )

    def appearance_times(self) -> list[datetime]:
        with self._lock:
            rows = self._conn.execute("SELECT seen_at FROM appearances").fetchall()
        return [datetime.fromisoformat(r[0]) for r in rows]
