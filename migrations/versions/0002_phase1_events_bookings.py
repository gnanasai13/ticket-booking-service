"""Add event administration and persisted bookings."""
from alembic import op
import sqlalchemy as sa

revision = "0002_phase1_events_bookings"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_check_constraint("ck_seat_nonnegative_price", "seats", "price_cents >= 0")
    op.create_table("bookings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="confirmed", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('confirmed','cancelled')", name="ck_booking_status"),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.UniqueConstraint("reference"),
        sa.UniqueConstraint("reservation_id"))
    for name, col in [("ix_bookings_reference", "reference"), ("ix_bookings_reservation_id", "reservation_id"),
                      ("ix_bookings_user_id", "user_id"), ("ix_bookings_event_id", "event_id")]:
        op.create_index(name, "bookings", [col])
    op.execute("""
        INSERT INTO bookings (reference, reservation_id, user_id, event_id, status)
        SELECT 'LEGACY-' || id::text, id, user_id, event_id, 'confirmed'
        FROM reservations WHERE status = 'confirmed'
    """)

def downgrade():
    op.drop_table("bookings")
    op.drop_constraint("ck_seat_nonnegative_price", "seats", type_="check")
    op.drop_column("users", "is_admin")
