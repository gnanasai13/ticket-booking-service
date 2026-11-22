"""Simple DB-backed expiry worker; multiple instances are safe via SKIP LOCKED."""
import time
from app.database import SessionLocal
from app.main import expire_due
from app.notifications import process_notifications

if __name__ == "__main__":
    while True:
        with SessionLocal() as db:
            try:
                count = expire_due(db)
                notifications = process_notifications(db)
                db.commit()
                if count:
                    print(f"expired {count} reservations", flush=True)
                for notification in notifications:
                    print(f"notification simulated: {notification['event_type']} "
                          f"booking={notification['booking_reference']}", flush=True)
            except Exception:
                db.rollback()
                raise
        time.sleep(2)
