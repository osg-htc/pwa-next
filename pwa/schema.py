"""Bring the database schema up to date (Alembic, from RHEL 9 AppStream).

`pwa init-db` and every other command call `upgrade` on start-up:

- an empty database gets every table at once and is marked as current;
- a database made before migrations existed (pwa-next 0.2.3 and older) is
  marked as the baseline, then upgraded;
- any other database is upgraded to the newest revision.

New tables and columns go into pwa/models.py AND a new file in
pwa/migrations/versions/; tests/test_schema.py checks that both give the
same schema.
"""
import os

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from .models import Base

BASELINE = "0001"
_HERE = os.path.dirname(os.path.abspath(__file__))


def _config(connection):
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(_HERE, "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


def head():
    return ScriptDirectory.from_config(_config(None)).get_current_head()


def current(engine):
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(engine):
    """Returns (revision before, revision after)."""
    with engine.begin() as conn:
        before = MigrationContext.configure(conn).get_current_revision()
        cfg = _config(conn)
        if before is None and not set(inspect(conn).get_table_names()) & {"hosts", "meshes", "users"}:
            Base.metadata.create_all(conn)
            command.stamp(cfg, "head")
        else:
            if before is None:
                command.stamp(cfg, BASELINE)
            command.upgrade(cfg, "head")
        after = MigrationContext.configure(conn).get_current_revision()
    return before, after
