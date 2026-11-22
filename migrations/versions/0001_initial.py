"""Initial booking schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("users", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False), sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("email"))
    op.create_index("ix_users_email", "users", ["email"])
    op.create_table("events", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("venue", sa.String(200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_published", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.create_index("ix_events_starts_at", "events", ["starts_at"])
    op.create_table("seats", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), nullable=False), sa.Column("label", sa.String(32), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", "label", name="uq_seat_event_label"))
    op.create_index("ix_seats_event_id", "seats", ["event_id"])
    op.create_table("reservations", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False), sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="held", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("idempotency_key", sa.String(128)),
        sa.CheckConstraint("status IN ('held','confirmed','cancelled','expired')", name="ck_reservation_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]), sa.ForeignKeyConstraint(["event_id"], ["events.id"]))
    for name, col in [("ix_reservations_user_id", "user_id"), ("ix_reservations_event_id", "event_id"), ("ix_reservations_expires_at", "expires_at")]:
        op.create_index(name, "reservations", [col])
    op.create_table("reservation_seats", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reservation_id", sa.Integer(), nullable=False), sa.Column("seat_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="held", nullable=False),
        sa.CheckConstraint("status IN ('held','confirmed','cancelled','expired')", name="ck_reservation_seat_status"),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["seat_id"], ["seats.id"]),
        sa.UniqueConstraint("reservation_id", "seat_id", name="uq_reservation_seat"))
    op.create_index("ix_reservation_seats_reservation_id", "reservation_seats", ["reservation_id"])
    op.create_index("ix_reservation_seats_seat_id", "reservation_seats", ["seat_id"])
    op.create_index("uq_live_seat_claim", "reservation_seats", ["seat_id"], unique=True,
                    postgresql_where=sa.text("status IN ('held','confirmed')"))
    op.create_table("idempotency_keys", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False), sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False), sa.Column("reservation_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"))

def downgrade():
    op.drop_table("idempotency_keys")
    op.drop_index("uq_live_seat_claim", table_name="reservation_seats")
    op.drop_table("reservation_seats")
    op.drop_table("reservations")
    op.drop_table("seats")
    op.drop_table("events")
    op.drop_table("users")
