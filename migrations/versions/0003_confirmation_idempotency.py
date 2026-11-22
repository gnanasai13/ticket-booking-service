"""Give hold and confirmation requests separate idempotency scopes."""
from alembic import op
import sqlalchemy as sa

revision = "0003_confirmation_idempotency"
down_revision = "0002_phase1_events_bookings"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("idempotency_keys", sa.Column("operation", sa.String(16), server_default="hold", nullable=False))
    op.drop_constraint("uq_idempotency_user_key", "idempotency_keys", type_="unique")
    op.create_unique_constraint("uq_idempotency_user_key_operation", "idempotency_keys", ["user_id", "key", "operation"])
    op.create_check_constraint("ck_idempotency_operation", "idempotency_keys", "operation IN ('hold','confirm')")

def downgrade():
    op.drop_constraint("ck_idempotency_operation", "idempotency_keys", type_="check")
    op.drop_constraint("uq_idempotency_user_key_operation", "idempotency_keys", type_="unique")
    op.drop_column("idempotency_keys", "operation")
    op.create_unique_constraint("uq_idempotency_user_key", "idempotency_keys", ["user_id", "key"])
