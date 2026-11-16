"""PostgreSQL session lifecycle. Local JSON files are never authoritative state."""

from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from collections.abc import Callable, Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from procurement.platform.env import env_int, load_local_env


logger = logging.getLogger("procurement.database")
_ROLLBACK_CALLBACKS = "procurement.rollback_callbacks"


class Base(DeclarativeBase):
    pass


def database_url() -> str:
    load_local_env()
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required for PostgreSQL-backed operations")
    return value


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    engine = create_engine(
        url or database_url(),
        pool_size=env_int("DATABASE_POOL_SIZE", 10, minimum=1),
        max_overflow=env_int("DATABASE_MAX_OVERFLOW", 20, minimum=0),
        pool_timeout=env_int("DATABASE_POOL_TIMEOUT_SECONDS", 30, minimum=1),
        pool_pre_ping=True,
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def on_rollback(session: Session, callback: Callable[[], None]) -> None:
    """Undo a non-transactional side effect if the surrounding DB commit fails."""
    session.info.setdefault(_ROLLBACK_CALLBACKS, []).append(callback)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
        session.info.pop(_ROLLBACK_CALLBACKS, None)
    except Exception:
        session.rollback()
        for callback in reversed(session.info.pop(_ROLLBACK_CALLBACKS, [])):
            try:
                callback()
            except Exception:
                logger.exception("Rollback cleanup failed")
        raise
    finally:
        session.close()
