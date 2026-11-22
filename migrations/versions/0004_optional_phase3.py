"""Add payment simulation attempts and notification outbox."""
from alembic import op
import sqlalchemy as sa

revision = "0004_optional_phase3"
down_revision = "0003_confirmation_idempotency"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("bookings", sa.Column("payment_status", sa.String(16), server_default="not_required", nullable=False))
    op.create_check_constraint("ck_booking_payment_status", "bookings", "payment_status IN ('not_required','pending','paid','failed')")
    op.create_table("payment_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("outcome IN ('succeeded','failed')", name="ck_payment_outcome"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("booking_id", "idempotency_key", name="uq_payment_booking_idempotency"))
    op.create_index("ix_payment_attempts_booking_id", "payment_attempts", ["booking_id"])
    op.create_table("notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('queued','simulated')", name="ck_notification_status"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"))
    op.create_index("ix_notification_outbox_booking_id", "notification_outbox", ["booking_id"])

def downgrade():
    op.drop_table("notification_outbox")
    op.drop_table("payment_attempts")
    op.drop_constraint("ck_booking_payment_status", "bookings", type_="check")
    op.drop_column("bookings", "payment_status")
