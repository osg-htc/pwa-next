"""Alembic environment for pwa-next.  Run through pwa.schema, never the alembic command."""
from alembic import context

from pwa.models import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata,
                  render_as_batch=connection.dialect.name == "sqlite")   # SQLite cannot ALTER much
with context.begin_transaction():
    context.run_migrations()
