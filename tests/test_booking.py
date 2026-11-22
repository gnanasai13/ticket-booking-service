from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Booking, Event, NotificationOutbox, PaymentAttempt, Reservation, ReservationSeat, User
from app.main import app

def test_concurrent_users_cannot_hold_same_seat(client, make_user, event_id):
    event, (seat_id, *_rest) = event_id
    tokens = [make_user(f"user{i}@example.com") for i in range(2)]
    def request(i):
        return client.post(f"/events/{event}/reservations", headers={"Authorization": f"Bearer {tokens[i]}",
                           "Idempotency-Key": f"key-{i}"}, json={"seat_ids": [seat_id]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert sorted(r.status_code for r in results) == [201, 409]
    winner = next(r for r in results if r.status_code == 201)
    token = tokens[results.index(winner)]
    confirmed = client.post(f"/reservations/{winner.json()['id']}/confirm",
                            headers={"Authorization": f"Bearer {token}"})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    loser_idx = 1 - results.index(winner)
    retry = client.post(f"/events/{event}/reservations", headers={"Authorization": f"Bearer {tokens[loser_idx]}",
                        "Idempotency-Key": "retry"}, json={"seat_ids": [seat_id]})
    assert retry.status_code == 409

def test_idempotency_and_key_payload_conflict(client, make_user, event_id):
    event, seats = event_id
    token = make_user("same@example.com")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "stable-key"}
    one = client.post(f"/events/{event}/reservations", headers=headers, json={"seat_ids": [seats[0]]})
    two = client.post(f"/events/{event}/reservations", headers=headers, json={"seat_ids": [seats[0]]})
    assert one.status_code == two.status_code == 201
    assert one.json()["id"] == two.json()["id"]
    different = client.post(f"/events/{event}/reservations", headers=headers, json={"seat_ids": [seats[1]]})
    assert different.status_code == 409

def test_cancellation_releases_seats_and_expiry_expires_holds(client, make_user, event_id, engine):
    event, seats = event_id
    token = make_user("cancel@example.com")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "cancel-me"}
    held = client.post(f"/events/{event}/reservations", headers=headers, json={"seat_ids": [seats[0]]})
    cancelled = client.post(f"/reservations/{held.json()['id']}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert cancelled.json()["status"] == "cancelled"
    again = client.post(f"/events/{event}/reservations", headers={"Authorization": f"Bearer {token}",
                       "Idempotency-Key": "new-key"}, json={"seat_ids": [seats[0]]})
    assert again.status_code == 201
    with Session(engine) as db:
        reservation = db.get(Reservation, again.json()["id"])
        reservation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    expired = client.post(f"/reservations/{again.json()['id']}/confirm", headers={"Authorization": f"Bearer {token}"})
    assert expired.status_code == 409
    with Session(engine) as db:
        assert db.get(Reservation, again.json()["id"]).status == "expired"
    assert client.get(f"/events/{event}/seats").json()[0]["available"] is True

def test_authentication_required_for_holds(client, event_id):
    event, (seat_id, *_rest) = event_id
    response = client.post(f"/events/{event}/reservations", headers={"Idempotency-Key": "x"}, json={"seat_ids": [seat_id]})
    assert response.status_code == 401

def test_event_admin_and_booking_history_lifecycle(client, make_user, engine):
    token = make_user("admin-phase1@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    payload = {"name": "Phase 1 show", "venue": "Hall 2", "starts_at": "2031-05-01T20:00:00Z",
               "seats": [{"label": "B-1", "price_cents": 2500}, {"label": "B-2", "price_cents": 3000}]}
    denied = client.post("/events", headers=auth, json=payload)
    assert denied.status_code == 403
    with Session(engine) as db:
        db.query(User).filter(User.email == "admin-phase1@example.com").update({"is_admin": True})
        db.commit()
    created = client.post("/events", headers=auth, json=payload)
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    updated = client.patch(f"/events/{event_id}", headers=auth,
                           json={"venue": "Updated Hall", "add_seats": [{"label": "B-3", "price_cents": 3500}]})
    assert updated.status_code == 200
    detail = client.get(f"/events/{event_id}")
    assert detail.status_code == 200
    assert detail.json()["venue"] == "Updated Hall"
    assert len(detail.json()["seats"]) == 3
    seat_id = detail.json()["seats"][0]["id"]
    hold = client.post(f"/events/{event_id}/reservations", headers={**auth, "Idempotency-Key": "phase1-booking"},
                       json={"seat_ids": [seat_id]})
    assert hold.status_code == 201
    assert client.get(f"/events/{event_id}/seats").json()[0]["status"] == "held"
    confirmed = client.post(f"/reservations/{hold.json()['id']}/confirm", headers=auth)
    assert confirmed.status_code == 200
    bookings = client.get("/bookings", headers=auth)
    assert bookings.status_code == 200
    booking = bookings.json()[0]
    assert len(booking["reference"]) == 32
    assert booking["status"] == "confirmed"
    assert client.get(f"/bookings/{booking['id']}", headers=auth).status_code == 200
    assert client.get(f"/bookings/{booking['id']}", headers={"Authorization": f"Bearer {make_user('other-phase1@example.com')}"}).status_code == 404
    assert client.get(f"/events/{event_id}/seats").json()[0]["status"] == "booked"
    cancelled = client.post(f"/bookings/{booking['id']}/cancel", headers=auth)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.get(f"/events/{event_id}/seats").json()[0]["status"] == "available"

def test_different_seats_can_be_reserved_concurrently(client, make_user, event_id):
    event, seats = event_id
    tokens = [make_user(f"parallel{i}@example.com") for i in range(2)]
    def request(i):
        return client.post(f"/events/{event}/reservations",
                           headers={"Authorization": f"Bearer {tokens[i]}", "Idempotency-Key": f"seat-{i}"},
                           json={"seat_ids": [seats[i]]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert [response.status_code for response in results] == [201, 201]
    def confirm(i):
        return client.post(f"/reservations/{results[i].json()['id']}/confirm",
                           headers={"Authorization": f"Bearer {tokens[i]}", "Idempotency-Key": f"confirm-seat-{i}"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmed = list(pool.map(confirm, range(2)))
    assert [response.status_code for response in confirmed] == [200, 200]

def test_confirmation_idempotency_replays_one_booking(client, make_user, event_id):
    event, seats = event_id
    token = make_user("confirm-retry@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    hold = client.post(f"/events/{event}/reservations", headers={**auth, "Idempotency-Key": "hold-key"},
                       json={"seat_ids": [seats[0]]})
    confirm_headers = {**auth, "Idempotency-Key": "confirm-key"}
    def confirm_once():
        return client.post(f"/reservations/{hold.json()['id']}/confirm", headers=confirm_headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm_once(), range(2)))
    assert [result.status_code for result in results] == [200, 200]
    assert results[0].json()["booking_reference"] == results[1].json()["booking_reference"]
    assert len(client.get("/bookings", headers=auth).json()) == 1
    second_hold = client.post(f"/events/{event}/reservations", headers={**auth, "Idempotency-Key": "hold-second"},
                              json={"seat_ids": [seats[1]]})
    conflict = client.post(f"/reservations/{second_hold.json()['id']}/confirm", headers=confirm_headers)
    assert conflict.status_code == 409
    assert client.get(f"/reservations/{second_hold.json()['id']}", headers=auth).json()["status"] == "held"

def test_booking_write_failure_rolls_back_confirmation(client, make_user, event_id, engine, monkeypatch):
    import app.main as main
    event, seats = event_id
    token = make_user("rollback@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    first_hold = client.post(f"/events/{event}/reservations", headers={**auth, "Idempotency-Key": "first"},
                             json={"seat_ids": [seats[0]]}).json()
    first = client.post(f"/reservations/{first_hold['id']}/confirm", headers=auth).json()
    second_hold = client.post(f"/events/{event}/reservations", headers={**auth, "Idempotency-Key": "second"},
                              json={"seat_ids": [seats[1]]}).json()
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "rollback@example.com"))
        monkeypatch.setattr(main.uuid, "uuid4", lambda: SimpleNamespace(hex=first["booking_reference"]))
        with pytest.raises(IntegrityError):
            main.confirm(second_hold["id"], None, user, db)
        db.rollback()
    with Session(engine) as db:
        reservation = db.get(Reservation, second_hold["id"])
        assert reservation.status == "held"
        assert db.scalar(select(ReservationSeat.status).where(ReservationSeat.reservation_id == reservation.id)) == "held"
        assert len(list(db.scalars(select(Booking)))) == 1

def test_request_id_metrics_and_redis_rate_limit(client, monkeypatch):
    class FakeRedis:
        async def eval(self, *_args):
            return [121, 37]
    monkeypatch.setattr(app.state, "redis", FakeRedis(), raising=False)
    limited = client.post("/auth/register", headers={"X-Request-ID": "trace.phase3"},
                          json={"email": "limited@example.com", "password": "rate-limit-password"})
    assert limited.status_code == 429
    assert limited.headers["X-Request-ID"] == "trace.phase3"
    assert limited.headers["Retry-After"] == "37"
    metric_response = client.get("/metrics")
    assert metric_response.status_code == 200
    assert "ticket_api_http_requests_total" in metric_response.text

def test_event_listing_cache_invalidates_and_does_not_cache_availability(client, monkeypatch, make_user, event_id, engine):
    class FakeRedis:
        def __init__(self):
            self.values = {}
            self.cache_reads = 0
        async def get(self, key):
            self.cache_reads += 1
            return self.values.get(key)
        async def setex(self, key, _ttl, value):
            self.values[key] = value
        async def delete(self, key):
            self.values.pop(key, None)
        async def eval(self, *_args):
            return [1, 60]
    fake = FakeRedis()
    monkeypatch.setattr(app.state, "redis", fake, raising=False)
    assert client.get("/events").status_code == 200
    assert client.get("/events").status_code == 200
    assert fake.cache_reads == 2
    assert len(fake.values) == 1
    token = make_user("cache-admin@example.com")
    with Session(engine) as db:
        db.query(User).filter(User.email == "cache-admin@example.com").update({"is_admin": True})
        db.commit()
    created = client.post("/events", headers={"Authorization": f"Bearer {token}"}, json={
        "name": "Cache invalidation show", "venue": "Hall", "starts_at": "2032-01-01T20:00:00Z",
        "seats": [{"label": "C-1", "price_cents": 1000}]})
    assert created.status_code == 201
    assert fake.values == {}
    assert len(client.get("/events").json()) == 2
    event, (seat_id, *_rest) = event_id
    hold = client.post(f"/events/{event}/reservations",
                       headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "cache-hold"},
                       json={"seat_ids": [seat_id]})
    assert hold.status_code == 201
    assert client.get(f"/events/{event}/seats").json()[0]["status"] == "held"
    client.post(f"/reservations/{hold.json()['id']}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert client.get(f"/events/{event}/seats").json()[0]["status"] == "available"

def test_simulated_payment_failure_can_retry_successfully(client, make_user, event_id):
    event, seats = event_id
    token = make_user("payment-sim@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    hold = client.post(f"/events/{event}/reservations", headers={**auth, "Idempotency-Key": "payment-hold"},
                       json={"seat_ids": [seats[0]]}).json()
    client.post(f"/reservations/{hold['id']}/confirm", headers=auth)
    booking = client.get("/bookings", headers=auth).json()[0]
    failed = client.post(f"/bookings/{booking['id']}/payments",
                         headers={**auth, "Idempotency-Key": "pay-attempt-1"},
                         json={"outcome": "failure"})
    assert failed.status_code == 201
    assert failed.json()["outcome"] == "failed"
    success = client.post(f"/bookings/{booking['id']}/payments",
                          headers={**auth, "Idempotency-Key": "pay-attempt-2"},
                          json={"outcome": "success"})
    assert success.status_code == 201
    assert success.json()["outcome"] == "succeeded"
    replay = client.post(f"/bookings/{booking['id']}/payments",
                         headers={**auth, "Idempotency-Key": "pay-attempt-2"},
                         json={"outcome": "success"})
    assert replay.json()["id"] == success.json()["id"]
    key_conflict = client.post(f"/bookings/{booking['id']}/payments",
                               headers={**auth, "Idempotency-Key": "pay-attempt-2"},
                               json={"outcome": "failure"})
    assert key_conflict.status_code == 409
    assert client.get(f"/bookings/{booking['id']}", headers=auth).json()["payment_status"] == "paid"
    assert len(client.get(f"/bookings/{booking['id']}/payments", headers=auth).json()) == 2

def test_booking_notifications_use_reliable_outbox(client, make_user, event_id, engine):
    from app.notifications import process_notifications
    event, (seat_id, *_rest) = event_id
    token = make_user("notify@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    hold = client.post(f"/events/{event}/reservations", headers={**auth, "Idempotency-Key": "notify-hold"},
                       json={"seat_ids": [seat_id]}).json()
    client.post(f"/reservations/{hold['id']}/confirm", headers=auth)
    with Session(engine) as db:
        pending = process_notifications(db)
        db.commit()
        assert [item["event_type"] for item in pending] == ["booking_confirmed"]
        assert list(db.scalars(select(NotificationOutbox.status))) == ["simulated"]
    booking_id = client.get("/bookings", headers=auth).json()[0]["id"]
    client.post(f"/bookings/{booking_id}/cancel", headers=auth)
    with Session(engine) as db:
        pending = process_notifications(db)
        db.commit()
        assert [item["event_type"] for item in pending] == ["booking_cancelled"]
        assert len(list(db.scalars(select(PaymentAttempt)))) == 0


def test_event_start_blocks_new_holds_and_expires_existing_holds(client, make_user, event_id, engine):
    event, seats = event_id
    token = make_user("event-start-boundary@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    held = client.post(
        f"/events/{event}/reservations",
        headers={**auth, "Idempotency-Key": "before-event-start"},
        json={"seat_ids": [seats[0]]},
    )
    assert held.status_code == 201

    with Session(engine) as db:
        event_row = db.get(Event, event)
        event_row.starts_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    new_hold = client.post(
        f"/events/{event}/reservations",
        headers={**auth, "Idempotency-Key": "after-event-start"},
        json={"seat_ids": [seats[1]]},
    )
    assert new_hold.status_code == 409

    confirmation = client.post(
        f"/reservations/{held.json()['id']}/confirm",
        headers=auth,
    )
    assert confirmation.status_code == 409
    with Session(engine) as db:
        reservation = db.get(Reservation, held.json()["id"])
        assert reservation.status == "expired"
