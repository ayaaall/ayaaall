"""
app/core/database.py
─────────────────────
Async database layer built on SQLAlchemy 2.0.

Runs against local SQLite by default (zero-config dev) and against
PostgreSQL when `DATABASE_URL` points to it (docker-compose).

Tables
──────
users     — registered accounts (identifiant + mot de passe + optional name)
sessions  — refresh-token records (revocation = logout)
jobs      — processing jobs, each owned by a user
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text, delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import DATABASE_URL, REFRESH_TOKEN_EXPIRE_DAYS
from app.core.security import hash_api_key, hash_refresh_token

log = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


# ── Models ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id:            Mapped[str]           = mapped_column(String(32), primary_key=True)
    identifiant:   Mapped[str]           = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str]           = mapped_column(String(255))
    name:          Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at:    Mapped[str]           = mapped_column(String(40), default=_utcnow)


class Session(Base):
    """Refresh-token record. `revoked` flips to 1 on logout / token rotation.

    `token` holds the SHA-256 digest of the refresh token, never the raw value.
    """

    __tablename__ = "sessions"

    token:      Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id:    Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=_utcnow)
    expires_at: Mapped[str] = mapped_column(String(40))
    revoked:    Mapped[int] = mapped_column(Integer, default=0)


class ApiKey(Base):
    """Long-lived credential for server-to-server use. Only the digest is stored."""

    __tablename__ = "api_keys"

    id:           Mapped[str]           = mapped_column(String(32), primary_key=True)
    user_id:      Mapped[str]           = mapped_column(ForeignKey("users.id"), index=True)
    name:         Mapped[str]           = mapped_column(String(80))
    key_hash:     Mapped[str]           = mapped_column(String(64), unique=True, index=True)
    prefix:       Mapped[str]           = mapped_column(String(16))   # display only
    created_at:   Mapped[str]           = mapped_column(String(40), default=_utcnow)
    last_used_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    revoked:      Mapped[int]           = mapped_column(Integer, default=0)


class Job(Base):
    __tablename__ = "jobs"

    id:               Mapped[str]           = mapped_column(String(32), primary_key=True)
    user_id:          Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action_type:      Mapped[str]           = mapped_column(String(32))
    status:           Mapped[str]           = mapped_column(String(16), default="pending")
    progress:         Mapped[int]           = mapped_column(Integer, default=0)
    created_at:       Mapped[str]           = mapped_column(String(40))
    primary_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    options:          Mapped[str]           = mapped_column(Text, default="{}")
    outputs:          Mapped[str]           = mapped_column(Text, default="[]")
    preview_text:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_files:     Mapped[str]           = mapped_column(Text, default="[]")


# ── Lifecycle ──────────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create database tables on first run (and patch older schemas)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database initialised (%s)", DATABASE_URL.split("://")[0])

    # Lightweight migration for databases created before source_files existed.
    # Run in its OWN transaction: Postgres aborts the entire transaction on
    # any failed statement (unlike SQLite), so if this ran in the same
    # transaction as create_all above, a harmless "column already exists"
    # error here would silently roll back the table creation too.
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "ALTER TABLE jobs ADD COLUMN source_files TEXT DEFAULT '[]'"
            )
        log.info("Added jobs.source_files column")
    except Exception:
        log.debug("jobs.source_files already present")  # column already exists

    await purge_expired_sessions()


# ── User helpers ───────────────────────────────────────────────────────────────

async def get_user_by_identifiant(identifiant: str) -> Optional[User]:
    async with SessionLocal() as db:
        row = await db.execute(select(User).where(User.identifiant == identifiant))
        return row.scalar_one_or_none()


async def get_user_by_id(user_id: str) -> Optional[User]:
    async with SessionLocal() as db:
        return await db.get(User, user_id)


async def create_user(user_id: str, identifiant: str, password_hash: str,
                      name: Optional[str]) -> User:
    user = User(id=user_id, identifiant=identifiant,
                password_hash=password_hash, name=name)
    async with SessionLocal() as db:
        db.add(user)
        await db.commit()
    return user


# ── Refresh-session helpers ────────────────────────────────────────────────────

async def create_session(raw_token: str, user_id: str) -> None:
    expires = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    async with SessionLocal() as db:
        db.add(Session(token=hash_refresh_token(raw_token), user_id=user_id,
                       expires_at=expires.isoformat()))
        await db.commit()


async def get_valid_session(raw_token: str) -> Optional[Session]:
    async with SessionLocal() as db:
        sess = await db.get(Session, hash_refresh_token(raw_token))
        if sess is None or sess.revoked:
            return None
        if datetime.fromisoformat(sess.expires_at) < datetime.now(timezone.utc):
            return None
        return sess


async def consume_session(raw_token: str) -> Optional[Session]:
    """Atomically revoke a refresh token and return its session.

    Returns None when the token is unknown, expired, or was already used —
    so two concurrent refreshes with the same token cannot both succeed.
    """
    digest = hash_refresh_token(raw_token)
    async with SessionLocal() as db:
        result = await db.execute(
            update(Session)
            .where(Session.token == digest, Session.revoked == 0)
            .values(revoked=1)
        )
        await db.commit()
        if result.rowcount != 1:
            return None
        sess = await db.get(Session, digest)
    if sess is None or datetime.fromisoformat(sess.expires_at) < datetime.now(timezone.utc):
        return None
    return sess


async def revoke_session(raw_token: str) -> None:
    async with SessionLocal() as db:
        await db.execute(
            update(Session).where(Session.token == hash_refresh_token(raw_token)).values(revoked=1)
        )
        await db.commit()


async def purge_expired_sessions() -> None:
    """Drop expired or revoked refresh sessions (housekeeping at startup)."""
    now = datetime.now(timezone.utc).isoformat()
    async with SessionLocal() as db:
        await db.execute(delete(Session).where((Session.expires_at < now) | (Session.revoked == 1)))
        await db.commit()


# ── API-key helpers ────────────────────────────────────────────────────────────

async def create_api_key(key_id: str, user_id: str, name: str, raw_key: str) -> ApiKey:
    row = ApiKey(id=key_id, user_id=user_id, name=name,
                 key_hash=hash_api_key(raw_key), prefix=raw_key[:12])
    async with SessionLocal() as db:
        db.add(row)
        await db.commit()
    return row


async def list_api_keys(user_id: str) -> list[ApiKey]:
    async with SessionLocal() as db:
        rows = await db.execute(
            select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked == 0)
            .order_by(ApiKey.created_at.desc())
        )
        return list(rows.scalars().all())


async def count_api_keys(user_id: str) -> int:
    async with SessionLocal() as db:
        q = select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked == 0)
        return (await db.execute(q)).scalar_one()


async def get_user_by_api_key(raw_key: str) -> Optional[User]:
    """Resolve an API key to its owner; records last use at most once a minute."""
    digest = hash_api_key(raw_key)
    async with SessionLocal() as db:
        row = (await db.execute(select(ApiKey).where(ApiKey.key_hash == digest))).scalar_one_or_none()
        if row is None or row.revoked:
            return None
        now = datetime.now(timezone.utc)
        last = datetime.fromisoformat(row.last_used_at) if row.last_used_at else None
        if last is None or (now - last).total_seconds() > 60:
            row.last_used_at = now.isoformat()
            await db.commit()
        return await db.get(User, row.user_id)


async def revoke_api_key(key_id: str, user_id: str) -> bool:
    async with SessionLocal() as db:
        result = await db.execute(
            update(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id, ApiKey.revoked == 0)
            .values(revoked=1)
        )
        await db.commit()
        return result.rowcount == 1


# ── Job helpers ────────────────────────────────────────────────────────────────

async def insert_job(job_id: str, user_id: Optional[str], action_type: str,
                     created_at: str, primary_filename: str, options: str,
                     source_files: str = "[]") -> None:
    async with SessionLocal() as db:
        db.add(Job(id=job_id, user_id=user_id, action_type=action_type,
                   created_at=created_at, primary_filename=primary_filename,
                   options=options, source_files=source_files))
        await db.commit()


async def get_job(job_id: str) -> Optional[Job]:
    async with SessionLocal() as db:
        return await db.get(Job, job_id)


async def count_jobs(user_id: Optional[str]) -> int:
    async with SessionLocal() as db:
        q = select(func.count()).select_from(Job)
        if user_id is not None:
            q = q.where(Job.user_id == user_id)
        return (await db.execute(q)).scalar_one()


async def list_jobs(user_id: Optional[str], limit: int, offset: int) -> list[Job]:
    async with SessionLocal() as db:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset)
        if user_id is not None:
            q = q.where(Job.user_id == user_id)
        rows = await db.execute(q)
        return list(rows.scalars().all())


async def delete_job(job_id: str, user_id: Optional[str] = None) -> bool:
    """Delete a job record. Returns True if a row owned by `user_id` was removed."""
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return False
        if user_id is not None and job.user_id != user_id:
            return False
        await db.delete(job)
        await db.commit()
        return True


async def update_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    outputs: Optional[list] = None,
    preview_text: Optional[str] = None,
) -> None:
    """Partial-update helper — only writes the fields that are provided."""
    values: dict = {}
    if status is not None:
        values["status"] = status
    if progress is not None:
        values["progress"] = progress
    if outputs is not None:
        values["outputs"] = json.dumps(outputs)
    if preview_text is not None:
        values["preview_text"] = preview_text

    if not values:
        return

    async with SessionLocal() as db:
        await db.execute(update(Job).where(Job.id == job_id).values(**values))
        await db.commit()
