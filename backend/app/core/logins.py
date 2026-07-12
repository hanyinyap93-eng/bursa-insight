"""Login-event log: one row per successful sign-in (Google or email+password).

Records who logged in, when, and how, so the site owner can see account
activity. Shares the same database engine as accounts (Postgres in prod, SQLite
locally), so the log persists across redeploys.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .users import _engine, _norm


class Base(DeclarativeBase):
    pass


class Login(Base):
    __tablename__ = "logins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, index=True)   # normalised lower-case
    name: Mapped[str] = mapped_column(String, default="")
    method: Mapped[str] = mapped_column(String)              # "google" | "email"
    at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True)


Base.metadata.create_all(_engine)


def record(email: str, name: str, method: str) -> None:
    """Append a login event. Best-effort: never raises into the auth path."""
    try:
        with Session(_engine) as s:
            s.add(Login(email=_norm(email), name=(name or "").strip(), method=method))
            s.commit()
    except Exception:  # noqa: BLE001 - logging a login must never break sign-in
        pass


def recent(limit: int = 500) -> list[dict]:
    """Return the most recent login events, newest first."""
    with Session(_engine) as s:
        rows = (s.query(Login)
                .order_by(Login.at.desc(), Login.id.desc())
                .limit(limit).all())
        return [{"email": r.email, "name": r.name, "method": r.method,
                 "at": r.at.replace(tzinfo=timezone.utc).isoformat() if r.at else None}
                for r in rows]
