from datetime import datetime, timedelta, timezone
from app.database import SessionLocal
from app.models import Event, Seat

with SessionLocal.begin() as db:
    event = Event(name="Sample Live Show", venue="Main Hall",
                  starts_at=datetime.now(timezone.utc) + timedelta(days=30))
    event.seats = [Seat(label=f"A-{n:02d}", price_cents=5000) for n in range(1, 51)]
    db.add(event)
    db.flush()
    print(f"Created event {event.id} with {len(event.seats)} seats")
