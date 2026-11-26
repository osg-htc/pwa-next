"""Follow-up with sites: who was contacted, when, how, and what came of it."""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "site_contacts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site", sa.String(255), nullable=False),
        sa.Column("hostname", sa.String(255)),
        sa.Column("contacted", sa.Date, nullable=False),
        sa.Column("via", sa.String(40)),
        sa.Column("reference", sa.String(500)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("created", sa.DateTime),
        sa.Column("created_by", sa.String(255)),
    )
    op.create_index("ix_site_contacts_site", "site_contacts", ["site"])


def downgrade():
    op.drop_index("ix_site_contacts_site", table_name="site_contacts")
    op.drop_table("site_contacts")
