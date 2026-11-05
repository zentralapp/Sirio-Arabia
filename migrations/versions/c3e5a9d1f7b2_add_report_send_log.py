"""add report_send_log

Revision ID: c3e5a9d1f7b2
Revises: a7d3b1e8c4f2
Create Date: 2026-10-29

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c3e5a9d1f7b2"
down_revision = "a7d3b1e8c4f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "report_send_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_key", sa.String(32), nullable=False),
        sa.Column("fecha_negocio", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("recipient", sa.String(255)),
        sa.Column("message_id", sa.String(128)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime()),
        sa.UniqueConstraint("report_key", "fecha_negocio", name="uq_report_send_log_key_fecha"),
        sa.CheckConstraint("status IN ('PENDING','SENT','FAILED')", name="ck_report_send_log_status"),
    )


def downgrade():
    op.drop_table("report_send_log")
