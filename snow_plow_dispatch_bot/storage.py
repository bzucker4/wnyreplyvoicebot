"""Persistence via SQLAlchemy Core: Supabase Postgres in production, SQLite for local dev and tests.

`businesses` belongs to the WNYReply SMS bot and is only read here. The voice_*
tables are created in production by migrations/*.sql; `create_schema()` exists
for SQLite dev/test databases only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)

# Owned by the SMS bot; mirrored here for reads (and for test/dev schemas).
businesses = sa.Table(
    "businesses", metadata,
    sa.Column("twilio_number", sa.Text, primary_key=True),
    sa.Column("business_name", sa.Text, nullable=False),
    sa.Column("service_area", sa.Text, nullable=False),
    sa.Column("booking_link", sa.Text, nullable=False),
    sa.Column("price_bands", sa.Text, nullable=False),
    sa.Column("contract_type", sa.Text, server_default="seasonal"),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("alert_phone", sa.Text),
    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
)

voice_settings = sa.Table(
    "voice_settings", metadata,
    sa.Column("twilio_number", sa.Text, sa.ForeignKey("businesses.twilio_number", ondelete="CASCADE"), primary_key=True),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("on_call_phone", sa.Text),
    sa.Column("callback_timeframe", sa.Text),
    sa.Column("owner_names", sa.Text),  # comma-separated
    sa.Column("service_towns", sa.Text),  # comma-separated; enables strict area checks
    sa.Column("service_zip_prefixes", sa.Text),  # comma-separated, e.g. "140,141,142"
    sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
)

voice_calls = sa.Table(
    "voice_calls", metadata,
    sa.Column("call_sid", sa.Text, primary_key=True),
    sa.Column("to_number", sa.Text, nullable=False, index=True),
    sa.Column("from_number", sa.Text, nullable=False),
    sa.Column("history", JSONType, nullable=False),
    sa.Column("no_input_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
)

voice_customers = sa.Table(
    "voice_customers", metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("business_number", sa.Text, sa.ForeignKey("businesses.twilio_number", ondelete="CASCADE"), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("phone", sa.Text),
    sa.Column("address", sa.Text, nullable=False),
    sa.Column("address_key", sa.Text, nullable=False),
    sa.Column("town", sa.Text),
    sa.Column("zip_code", sa.Text),
    sa.Column("property_type", sa.Text),
    sa.Column("plan", sa.Text),
    sa.Column("notes", sa.Text),
    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    sa.Index("voice_customers_phone_idx", "business_number", "phone"),
    sa.Index("voice_customers_address_idx", "business_number", "address_key"),
)

voice_tickets = sa.Table(
    "voice_tickets", metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("business_number", sa.Text, sa.ForeignKey("businesses.twilio_number", ondelete="CASCADE"), nullable=False),
    sa.Column("call_sid", sa.Text),
    sa.Column("call_type", sa.Text, nullable=False),
    sa.Column("priority", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("customer_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.ForeignKey("voice_customers.id", ondelete="SET NULL")),
    sa.Column("caller_phone", sa.Text),
    sa.Column("caller_name", sa.Text),
    sa.Column("callback_number", sa.Text),
    sa.Column("preferred_contact", sa.Text),
    sa.Column("property_type", sa.Text),
    sa.Column("service_type", sa.Text),
    sa.Column("contract_type", sa.Text),
    sa.Column("address", sa.Text),
    sa.Column("town", sa.Text),
    sa.Column("zip_code", sa.Text),
    sa.Column("access_notes", sa.Text),
    sa.Column("details", sa.Text),
    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    sa.Index("voice_tickets_open_idx", "business_number", "status"),
)

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CallSession:
    call_sid: str
    to_number: str
    caller_phone: str
    messages: list[dict[str, Any]]
    no_input_count: int
    status: str


def make_engine(url: str) -> sa.Engine:
    if url.startswith("sqlite"):
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            kwargs["poolclass"] = sa.StaticPool  # one shared in-memory DB
        return sa.create_engine(url, **kwargs)
    # Supabase's transaction pooler (port 6543) can't use server-side prepared statements.
    return sa.create_engine(url, pool_pre_ping=True, connect_args={"prepare_threshold": None})


class Database:
    def __init__(self, engine: sa.Engine) -> None:
        self.engine = engine

    @classmethod
    def from_url(cls, url: str) -> "Database":
        return cls(make_engine(url))

    def create_schema(self) -> None:
        """Dev/test only. Production schema comes from migrations/."""
        metadata.create_all(self.engine)

    def _rows(self, stmt: sa.Executable) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(stmt)]

    def _row(self, stmt: sa.Executable) -> dict[str, Any] | None:
        rows = self._rows(stmt)
        return rows[0] if rows else None

    # -- businesses ----------------------------------------------------------

    def get_business(self, twilio_number: str) -> dict[str, Any] | None:
        vs = voice_settings
        stmt = (
            sa.select(
                businesses,
                vs.c.enabled.label("voice_enabled"),
                vs.c.on_call_phone, vs.c.callback_timeframe, vs.c.owner_names,
                vs.c.service_towns, vs.c.service_zip_prefixes,
            )
            .select_from(businesses.outerjoin(vs, vs.c.twilio_number == businesses.c.twilio_number))
            .where(businesses.c.twilio_number == twilio_number)
        )
        return self._row(stmt)

    # -- customers -----------------------------------------------------------

    def add_customer(self, business_number: str, name: str, address: str, **fields: Any) -> dict[str, Any]:
        values = {"business_number": business_number, "name": name, "address": address,
                  "address_key": address_key(address), **fields}
        with self.engine.begin() as c:
            row = c.execute(voice_customers.insert().values(**values).returning(voice_customers)).one()
        return dict(row._mapping)

    def find_customers(self, business_number: str, phone: str | None = None,
                       address: str | None = None) -> list[dict[str, Any]]:
        conds = []
        if phone:
            conds.append(voice_customers.c.phone == phone)
        if address:
            conds.append(voice_customers.c.address_key == address_key(address))
        if not conds:
            return []
        return self._rows(
            sa.select(voice_customers)
            .where(voice_customers.c.business_number == business_number, sa.or_(*conds))
            .order_by(voice_customers.c.id).limit(5)
        )

    def list_customers(self, business_number: str | None = None) -> list[dict[str, Any]]:
        stmt = sa.select(voice_customers).order_by(voice_customers.c.id)
        if business_number:
            stmt = stmt.where(voice_customers.c.business_number == business_number)
        return self._rows(stmt)

    # -- tickets -------------------------------------------------------------

    def create_ticket(self, business_number: str, **fields: Any) -> dict[str, Any]:
        now = _now()
        values = {**fields, "business_number": business_number, "status": "new",
                  "created_at": now, "updated_at": now}
        with self.engine.begin() as c:
            row = c.execute(voice_tickets.insert().values(**values).returning(voice_tickets)).one()
        return dict(row._mapping)

    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        return self._row(sa.select(voice_tickets).where(voice_tickets.c.id == ticket_id))

    def open_tickets(self, business_number: str | None = None, caller_phone: str | None = None,
                     customer_id: int | None = None) -> list[dict[str, Any]]:
        """Open tickets, Priority One first then oldest first."""
        t = voice_tickets
        stmt = sa.select(t).where(t.c.status.in_(OPEN_TICKET_STATUSES))
        if business_number:
            stmt = stmt.where(t.c.business_number == business_number)
        if caller_phone is not None or customer_id is not None:
            stmt = stmt.where(sa.or_(t.c.caller_phone == caller_phone, t.c.customer_id == customer_id))
        stmt = stmt.order_by(sa.case((t.c.priority == "priority_one", 0), else_=1), t.c.id)
        return self._rows(stmt)

    def update_ticket_status(self, ticket_id: int, status: str) -> dict[str, Any] | None:
        if status not in TICKET_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self.engine.begin() as c:
            c.execute(voice_tickets.update().where(voice_tickets.c.id == ticket_id)
                      .values(status=status, updated_at=_now()))
        return self.get_ticket(ticket_id)

    def tickets_for_call(self, call_sid: str) -> list[dict[str, Any]]:
        return self._rows(sa.select(voice_tickets).where(voice_tickets.c.call_sid == call_sid)
                          .order_by(voice_tickets.c.id))

    # -- call sessions -------------------------------------------------------

    def get_or_create_session(self, call_sid: str, to_number: str, caller_phone: str) -> CallSession:
        select = sa.select(voice_calls).where(voice_calls.c.call_sid == call_sid)
        with self.engine.connect() as c:
            row = c.execute(select).first()
        if row is None:
            now = _now()
            try:
                with self.engine.begin() as c:
                    c.execute(voice_calls.insert().values(
                        call_sid=call_sid, to_number=to_number, from_number=caller_phone, history=[],
                        no_input_count=0, status=SESSION_ACTIVE, created_at=now, updated_at=now,
                    ))
            except sa.exc.IntegrityError:
                pass  # another webhook for this call created it first
            with self.engine.connect() as c:
                row = c.execute(select).one()
        m = row._mapping
        return CallSession(
            call_sid=m["call_sid"], to_number=m["to_number"], caller_phone=m["from_number"],
            messages=list(m["history"]), no_input_count=m["no_input_count"], status=m["status"],
        )

    def save_session(self, session: CallSession) -> None:
        with self.engine.begin() as c:
            c.execute(
                voice_calls.update().where(voice_calls.c.call_sid == session.call_sid).values(
                    history=session.messages, no_input_count=session.no_input_count,
                    status=session.status, updated_at=_now(),
                )
            )
