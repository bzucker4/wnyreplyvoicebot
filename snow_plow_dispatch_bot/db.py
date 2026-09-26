"""SQLite persistence for customers, service requests, and live call sessions."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    default_address TEXT,
    town TEXT,
    zip_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    call_sid TEXT,
    service_address TEXT NOT NULL,
    town TEXT NOT NULL,
    zip_code TEXT,
    service_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    notes TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requests_status ON service_requests(status);

CREATE TABLE IF NOT EXISTS call_sessions (
    call_sid TEXT PRIMARY KEY,
    caller_phone TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    no_input_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

OPEN_STATUSES = ("queued", "assigned", "en_route")
ALL_STATUSES = OPEN_STATUSES + ("completed", "cancelled")
SERVICE_TYPES = ("driveway", "parking_lot", "sidewalk", "salting")
PRIORITIES = ("standard", "urgent")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CallSession:
    call_sid: str
    caller_phone: str
    messages: list[dict[str, Any]]
    no_input_count: int
    status: str


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # -- customers -----------------------------------------------------------

    def get_customer_by_phone(self, phone: str) -> dict[str, Any] | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None

    def upsert_customer(
        self, phone: str, name: str, address: str, town: str, zip_code: str | None
    ) -> dict[str, Any]:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO customers (phone, name, default_address, town, zip_code, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    name = excluded.name,
                    default_address = excluded.default_address,
                    town = excluded.town,
                    zip_code = COALESCE(excluded.zip_code, customers.zip_code)
                """,
                (phone, name, address, town, zip_code, utcnow()),
            )
            row = c.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
        return dict(row)

    # -- service requests ----------------------------------------------------

    def create_request(
        self,
        customer_id: int,
        call_sid: str | None,
        service_address: str,
        town: str,
        zip_code: str | None,
        service_type: str,
        priority: str,
        notes: str | None,
    ) -> dict[str, Any]:
        now = utcnow()
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO service_requests
                    (customer_id, call_sid, service_address, town, zip_code, service_type,
                     priority, notes, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (customer_id, call_sid, service_address, town, zip_code, service_type,
                 priority, notes, now, now),
            )
            row = c.execute(
                "SELECT * FROM service_requests WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        with self._tx() as c:
            row = c.execute(
                """
                SELECT r.*, c.name AS customer_name, c.phone AS customer_phone
                FROM service_requests r JOIN customers c ON c.id = r.customer_id
                WHERE r.id = ?
                """,
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_requests_for_phone(self, phone: str, open_only: bool = False) -> list[dict[str, Any]]:
        sql = """
            SELECT r.* FROM service_requests r JOIN customers c ON c.id = r.customer_id
            WHERE c.phone = ?
        """
        params: list[Any] = [phone]
        if open_only:
            sql += f" AND r.status IN ({','.join('?' * len(OPEN_STATUSES))})"
            params.extend(OPEN_STATUSES)
        sql += " ORDER BY r.id DESC LIMIT 10"
        with self._tx() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def open_queue(self) -> list[dict[str, Any]]:
        """Open requests in dispatch order: urgent first, then oldest first."""
        with self._tx() as c:
            rows = c.execute(
                f"""
                SELECT r.*, c.name AS customer_name, c.phone AS customer_phone
                FROM service_requests r JOIN customers c ON c.id = r.customer_id
                WHERE r.status IN ({','.join('?' * len(OPEN_STATUSES))})
                ORDER BY CASE r.priority WHEN 'urgent' THEN 0 ELSE 1 END, r.id
                """,
                OPEN_STATUSES,
            ).fetchall()
        return [dict(r) for r in rows]

    def update_request_status(self, request_id: int, status: str) -> dict[str, Any] | None:
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._tx() as c:
            c.execute(
                "UPDATE service_requests SET status = ?, updated_at = ? WHERE id = ?",
                (status, utcnow(), request_id),
            )
        return self.get_request(request_id)

    # -- call sessions -------------------------------------------------------

    def get_or_create_session(self, call_sid: str, caller_phone: str) -> CallSession:
        now = utcnow()
        with self._tx() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO call_sessions
                    (call_sid, caller_phone, messages_json, no_input_count, status, created_at, updated_at)
                VALUES (?, ?, '[]', 0, 'active', ?, ?)
                """,
                (call_sid, caller_phone, now, now),
            )
            row = c.execute("SELECT * FROM call_sessions WHERE call_sid = ?", (call_sid,)).fetchone()
        return CallSession(
            call_sid=row["call_sid"],
            caller_phone=row["caller_phone"],
            messages=json.loads(row["messages_json"]),
            no_input_count=row["no_input_count"],
            status=row["status"],
        )

    def save_session(self, session: CallSession) -> None:
        with self._tx() as c:
            c.execute(
                """
                UPDATE call_sessions
                SET messages_json = ?, no_input_count = ?, status = ?, updated_at = ?
                WHERE call_sid = ?
                """,
                (json.dumps(session.messages), session.no_input_count, session.status,
                 utcnow(), session.call_sid),
            )
