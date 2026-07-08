"""Email + password user accounts (Postgres in prod, SQLite locally).

Passwords are hashed with bcrypt (used directly — passlib 1.7.4 is broken against
bcrypt 5.x). The store uses a hosted Postgres database when DATABASE_URL is set
(so accounts are durable on a cloud host), and falls back to a local SQLite file
otherwise — so local development needs no database server.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from sqlalchemy import Boolean, DateTime, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def _db_url() -> str:
    """Resolve the database URL. Prefer a hosted Postgres via DATABASE_URL (set
    automatically on Render), else a local SQLite file."""
    url = os.environ.get("DATABASE_URL") or os.environ.get("BURSA_DATABASE_URL")
    if url:
        # Render/Heroku hand out a 'postgres://' scheme that SQLAlchemy rejects;
        # normalise to the psycopg2 driver form.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg2://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        return url
    path = os.environ.get("BURSA_DB_PATH") or str(
        Path(__file__).resolve().parents[1] / "bursa.db")
    return f"sqlite:///{path}"


_URL = _db_url()
# SQLite needs check_same_thread=False to share across the scheduler + request
# threads; Postgres doesn't. pool_pre_ping recycles connections dropped by the
# host (common on free Postgres tiers) so requests don't fail after idle.
_connect_args = {"check_same_thread": False} if _URL.startswith("sqlite") else {}
_engine = create_engine(_URL, connect_args=_connect_args, pool_pre_ping=True)
_lock = threading.Lock()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String, primary_key=True)   # normalised lower-case
    name: Mapped[str] = mapped_column(String, default="")
    password_hash: Mapped[str] = mapped_column(String)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(_engine)


def _ensure_schema() -> None:
    """Add the `verified` column to a pre-existing table (create_all won't ALTER).
    Only runs for older SQLite DBs; fresh Postgres tables already have it. Existing
    rows are grandfathered as verified so current accounts aren't locked out."""
    insp = inspect(_engine)
    cols = [c["name"] for c in insp.get_columns("users")]
    if "verified" not in cols:
        is_pg = _engine.dialect.name == "postgresql"
        default, truthy = ("FALSE", "TRUE") if is_pg else ("0", "1")
        with _engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN verified BOOLEAN NOT NULL DEFAULT {default}"))
            conn.execute(text(f"UPDATE users SET verified = {truthy}"))


_ensure_schema()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _hash(password: str) -> str:
    # bcrypt caps at 72 bytes; truncate explicitly (bcrypt 5.x no longer does).
    pw = (password or "").encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("ascii")


def _check(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw((password or "").encode("utf-8")[:72],
                              hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _as_dict(u: "User") -> dict:
    return {"email": u.email, "name": u.name, "verified": bool(u.verified)}


def create_user(email: str, password: str, name: str = "", verified: bool = False) -> dict:
    """Create an account, or raise ValueError (bad input / email taken).
    New accounts are unverified until they confirm via the email link."""
    email = _norm(email)
    if not _EMAIL_RE.match(email):
        raise ValueError("a valid email address is required")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    with _lock, Session(_engine) as s:
        if s.get(User, email) is not None:
            raise ValueError("an account with this email already exists")
        u = User(email=email, name=(name or "").strip(),
                 password_hash=_hash(password), verified=verified)
        s.add(u)
        s.commit()
        return _as_dict(u)


def get_user(email: str) -> dict | None:
    with Session(_engine) as s:
        u = s.get(User, _norm(email))
        return _as_dict(u) if u else None


def mark_verified(email: str) -> bool:
    """Flip a user's verified flag on. Returns False if no such user."""
    with _lock, Session(_engine) as s:
        u = s.get(User, _norm(email))
        if u is None:
            return False
        if not u.verified:
            u.verified = True
            s.commit()
        return True


def authenticate(email: str, password: str) -> dict:
    """Return the user on correct credentials, else raise ValueError.

    Uses one generic message for both 'no such user' and 'wrong password' so the
    endpoint doesn't reveal which emails are registered. The caller checks the
    returned 'verified' flag to decide whether to allow sign-in.
    """
    email = _norm(email)
    with Session(_engine) as s:
        u = s.get(User, email)
    if u is None or not _check(password, u.password_hash):
        raise ValueError("incorrect email or password")
    return _as_dict(u)
