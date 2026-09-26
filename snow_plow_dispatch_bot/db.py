"""SQLite persistence for customers, call tickets, and live call sessions."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    address TEXT NOT NULL,
    address_key TEXT NOT NULL,
    town TEXT,
    zip_code TEXT,
    property_type TEXT,
    plan TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);
CREATE INDEX IF NOT EXISTS idx_customers_address ON customers(address_key);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid TEXT,
    call_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    customer_id INTEGER REFERENCES customers(id),
    caller_phone TEXT,
    caller_name TEXT,
    callback_number TEXT,
    preferred_contact TEXT,
    property_type TEXT,
    service_type TEXT,
    contract_type TEXT,
    address TEXT,
    town TEXT,
    zip_code TEXT,
    access_notes TEXT,
    details TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

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

CALL_TYPES = ("new_lead", "existing_customer", "complaint", "emergency")
PRIORITIES = ("normal", "priority_one")
TICKET_STATUSES = ("new", "contacted", "scheduled", "closed")
OPEN_TICKET_STATUSES = ("new", "contacted", "scheduled")
PROPERTY_TYPES = ("residential", "commercial")
SERVICE_TYPES = ("driveway_plowing", "lot_plowing", "sidewalk_clearing", "deicing")
CONTRACT_TYPES = ("seasonal", "one_time", "undecided")
CONTACT_METHODS = ("call", "text", "email")

# Call session states. "active" at hang-up means the call ended without the
# agent finishing it (caller hung up, line dropped, or silence timeout).
SESSION_ACTIVE, SESSION_COMPLETED, SESSION_TRANSFERRED, SESSION_DROPPED = (
    "active", "completed", "transferred", "dropped"
)

_ADDRESS_WORDS = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "court": "ct", "place": "pl", "boulevard": "blvd", "parkway": "pkwy",
    "circle": "cir", "terrace": "ter", "highway": "hwy", "north": "n", "south": "s",
    "east": "e", "west": "w",
}


def address_key(address: str) -> str:
    """Loose match key so '42 Elmwood Avenue' and '42 elmwood ave.' compare equal."""
    words = re.sub(r"[^a-z0-9 ]", " ", address.lower()).split()
    return " ".join(_ADDRESS_WORDS.get(w, w) for w in words)


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

    def add_customer(
        self,
        name: str,
        address: str,
        phone: str | None = None,
        town: str | None = None,
        zip_code: str | None = None,
        property_type: str | None = None,
        plan: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO customers
                    (name, phone, address, address_key, town, zip_code, property_type, plan, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, phone, address, address_key(address), town, zip_code, property_type, plan,
                 notes, utcnow()),
            )
            row = c.execute("SELECT * FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def find_customers(self, phone: str | None = None, address: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if phone:
            clauses.append("phone = ?")
            params.append(phone)
        if address:
            clauses.append("address_key = ?")
            params.append(address_key(address))
        if not clauses:
            return []
        with self._tx() as c:
            rows = c.execute(
                f"SELECT * FROM customers WHERE {' OR '.join(clauses)} ORDER BY id LIMIT 5", params
            ).fetchall()
        return [dict(r) for r in rows]

    def list_customers(self) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM customers ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # -- tickets -------------------------------------------------------------

    def create_ticket(self, **fields: Any) -> dict[str, Any]:
        now = utcnow()
        fields = {**fields, "status": "new", "created_at": now, "updated_at": now}
        cols = ", ".join(fields)
        with self._tx() as c:
            cur = c.execute(
                f"INSERT INTO tickets ({cols}) VALUES ({', '.join('?' * len(fields))})",
                list(fields.values()),
            )
            row = c.execute("SELECT * FROM tickets WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def open_tickets(self, caller_phone: str | None = None, customer_id: int | None = None) -> list[dict[str, Any]]:
        """Open tickets, Priority One first then oldest first; optionally for one caller or customer."""
        sql = f"SELECT * FROM tickets WHERE status IN ({','.join('?' * len(OPEN_TICKET_STATUSES))})"
        params: list[Any] = list(OPEN_TICKET_STATUSES)
        if caller_phone is not None or customer_id is not None:
            sql += " AND (caller_phone = ? OR customer_id = ?)"
            params += [caller_phone, customer_id]
        sql += " ORDER BY CASE priority WHEN 'priority_one' THEN 0 ELSE 1 END, id"
        with self._tx() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def update_ticket_status(self, ticket_id: int, status: str) -> dict[str, Any] | None:
        if status not in TICKET_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._tx() as c:
            c.execute(
                "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
                (status, utcnow(), ticket_id),
            )
        return self.get_ticket(ticket_id)

    def tickets_for_call(self, call_sid: str) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM tickets WHERE call_sid = ? ORDER BY id", (call_sid,)).fetchall()
        return [dict(r) for r in rows]

    # -- call sessions -------------------------------------------------------

    def get_or_create_session(self, call_sid: str, caller_phone: str) -> CallSession:
        now = utcnow()
        with self._tx() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO call_sessions
                    (call_sid, caller_phone, messages_json, no_input_count, status, created_at, updated_at)
                VALUES (?, ?, '[]', 0, ?, ?, ?)
                """,
                (call_sid, caller_phone, SESSION_ACTIVE, now, now),
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
