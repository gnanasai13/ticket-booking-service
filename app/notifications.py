"""Database outbox processor for local notification simulation."""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Booking, NotificationOutbox

def process_notifications(db: Session, batch_size: int = 50) -> list[dict[str, str]]:
    rows = list(db.scalars(select(NotificationOutbox)
                           .where(NotificationOutbox.status == "queued")
                           .order_by(NotificationOutbox.id)
                           .limit(batch_size).with_for_update(skip_locked=True)))
    output = []
    for row in rows:
        booking = db.get(Booking, row.booking_id)
        row.status = "simulated"
        row.processed_at = datetime.now(timezone.utc)
        output.append({"event_type": row.event_type,
                       "booking_reference": booking.reference if booking else "unknown"})
    return output
