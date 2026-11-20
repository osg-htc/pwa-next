"""Engine and session handling."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import scoped_session, sessionmaker

from .models import Base


def make_engine(url):
    kwargs = {"future": False}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url == "sqlite://":
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):      # SQLite ignores ON DELETE CASCADE unless asked
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


def make_session_factory(engine):
    return scoped_session(sessionmaker(bind=engine, autoflush=True, expire_on_commit=False))


def create_all(engine):
    Base.metadata.create_all(engine)
