from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import time
import uuid

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pwdlib import PasswordHash
from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import (Booking, Event, IdempotencyKey, NotificationOutbox, PaymentAttempt,
                        Reservation, ReservationSeat, Seat, User)
from app.schemas import (BookingOut, EventCreate, EventDetail, EventOut, EventUpdate,
                         HoldIn, LoginIn, PaymentIn, PaymentOut, RegisterIn,
                         ReservationOut, SeatOut, TokenOut)

password_hash = PasswordHash.recommended()
HTTP_REQUESTS = Counter("ticket_api_http_requests_total", "HTTP requests", ["method", "status"])
HTTP_DURATION = Histogram("ticket_api_http_request_duration_seconds", "HTTP request duration", ["method"])


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                           "level": record.levelname.lower(), "message": record.getMessage(),
                           **getattr(record, "fields", {})}, separators=(",", ":"))


logger = logging.getLogger("ticket_api")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Optional convenience for tests/local hacking; deployments use Alembic.
    if settings.auto_create_schema:
        Base.metadata.create_all(bind=engine)
    redis_client = None
    if settings.redis_url:
        redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis_client.ping()
        app.state.redis = redis_client
    yield
    if redis_client is not None:
        await redis_client.aclose()


app = FastAPI(title="Ticket Booking API", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_observability_and_rate_limit(request: Request, call_next):
    raw_request_id = request.headers.get("X-Request-ID", "")
    request_id = raw_request_id if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", raw_request_id) else uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    response = None
    redis_client = getattr(request.app.state, "redis", None)
    rate_limited_request = (redis_client is not None and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                            and request.url.path not in {"/health", "/health/live", "/health/ready", "/metrics"})
    if rate_limited_request:
        remote = request.client.host if request.client else "unknown"
        bucket = int(time.time()) // 60
        key = f"ticket-booking:rate:{remote}:{bucket}"
        try:
            count, ttl = await redis_client.eval(
                "local n=redis.call('INCR',KEYS[1]); "
                "if n==1 then redis.call('EXPIRE',KEYS[1],65) end; "
                "return {n,redis.call('TTL',KEYS[1])}", 1, key)
            if int(count) > settings.rate_limit_requests_per_minute:
                response = JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"},
                                        headers={"Retry-After": str(max(int(ttl), 1))})
        except Exception as exc:
            # Fail closed on write traffic if the configured limiter is unavailable.
            logger.exception("rate_limiter_unavailable", extra={"fields": {"request_id": request_id,
                             "method": request.method, "error": type(exc).__name__}})
            response = JSONResponse(status_code=503, content={"detail": "Rate limiting service unavailable"})
    if response is None:
        redis_client = getattr(request.app.state, "redis", None)
        event_list_key = "ticket-booking:cache:upcoming-events:v1"
        cacheable_event_list = request.method == "GET" and request.url.path == "/events"
        if redis_client is not None and cacheable_event_list:
            try:
                cached = await redis_client.get(event_list_key)
                if cached is not None:
                    response = JSONResponse(content=json.loads(cached))
            except Exception as exc:
                logger.warning("event_cache_read_failed", extra={"fields": {"request_id": request_id,
                               "error": type(exc).__name__}})
        if response is None:
            response = await call_next(request)
            if redis_client is not None and cacheable_event_list and response.status_code == 200:
                try:
                    body = b"".join([chunk async for chunk in response.body_iterator])
                    original_headers = dict(response.headers)
                    original_media_type = response.media_type
                    response = Response(content=body, status_code=response.status_code,
                                        headers=original_headers, media_type=original_media_type)
                except Exception as exc:
                    logger.warning("event_cache_write_failed", extra={"fields": {"request_id": request_id,
                                   "error": type(exc).__name__}})
                else:
                    try:
                        await redis_client.setex(event_list_key, settings.event_cache_ttl_seconds, body.decode())
                    except Exception as exc:
                        logger.warning("event_cache_write_failed", extra={"fields": {"request_id": request_id,
                                       "error": type(exc).__name__}})
            if (redis_client is not None and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                    and (request.url.path == "/events" or request.url.path.startswith("/events/"))):
                try:
                    await redis_client.delete(event_list_key)
                except Exception as exc:
                    logger.warning("event_cache_invalidation_failed", extra={"fields": {"request_id": request_id,
                                   "error": type(exc).__name__}})
    elapsed = time.perf_counter() - started
    response.headers["X-Request-ID"] = request_id
    HTTP_REQUESTS.labels(request.method, str(response.status_code)).inc()
    HTTP_DURATION.labels(request.method).observe(elapsed)
    route = request.scope.get("route")
    logger.info("http_request", extra={"fields": {"request_id": request_id,
                 "method": request.method, "path": getattr(route, "path", request.url.path),
                 "status": response.status_code, "duration_ms": round(elapsed * 1000, 2)}})
    return response


def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def make_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user.id), "iat": now,
                       "exp": now + timedelta(minutes=settings.access_token_minutes)},
                      settings.jwt_secret, algorithm="HS256")


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(db_session)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(authorization[7:], settings.jwt_secret, algorithms=["HS256"])
        uid = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(401, "Invalid or expired access token", headers={"WWW-Authenticate": "Bearer"})
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(401, "Invalid access token")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Administrator access required")
    return user


def reservation_out(reservation: Reservation) -> ReservationOut:
    return ReservationOut(id=reservation.id, event_id=reservation.event_id,
                          status=reservation.status, expires_at=reservation.expires_at,
                          seat_ids=[s.seat_id for s in reservation.seats],
                          booking_reference=reservation.booking.reference if reservation.booking else None)


def booking_out(booking: Booking) -> BookingOut:
    return BookingOut(id=booking.id, reference=booking.reference,
                      reservation_id=booking.reservation_id, event_id=booking.event_id,
                      status=booking.status, created_at=booking.created_at,
                      cancelled_at=booking.cancelled_at,
                      seat_ids=[claim.seat_id for claim in booking.reservation.seats],
                      payment_status=booking.payment_status)


def queue_booking_notification(db: Session, booking: Booking, email: str, event_type: str) -> None:
    db.add(NotificationOutbox(booking_id=booking.id, recipient_email=email,
                              event_type=event_type,
                              payload={"booking_reference": booking.reference}))


def payment_out(attempt: PaymentAttempt) -> PaymentOut:
    return PaymentOut(id=attempt.id, booking_id=attempt.booking_id, outcome=attempt.outcome,
                      amount_cents=attempt.amount_cents, created_at=attempt.created_at)


def seat_listing(db: Session, event_id: int) -> list[SeatOut]:
    now = datetime.now(timezone.utc)
    claims = db.execute(select(ReservationSeat.seat_id, ReservationSeat.status)
                        .join(Reservation).where(Reservation.event_id == event_id,
                            ReservationSeat.status.in_(["held", "confirmed"]),
                            (ReservationSeat.status == "confirmed") | (Reservation.expires_at > now))).all()
    state = {seat_id: ("booked" if claim_status == "confirmed" else "held")
             for seat_id, claim_status in claims}
    seats = db.scalars(select(Seat).where(Seat.event_id == event_id).order_by(Seat.label))
    return [SeatOut(id=seat.id, label=seat.label, price_cents=seat.price_cents,
                    status=state.get(seat.id, "available"), available=seat.id not in state)
            for seat in seats]


def expire_due(db: Session, event_id: int | None = None) -> int:
    now = datetime.now(timezone.utc)
    query = (
        select(Reservation)
        .join(Event, Event.id == Reservation.event_id)
        .where(
            Reservation.status == "held",
            or_(Reservation.expires_at <= now, Event.starts_at <= now),
        )
    )
    if event_id is not None:
        query = query.where(Reservation.event_id == event_id)
    due = list(db.scalars(query.with_for_update(skip_locked=True)))
    for reservation in due:
        reservation.status = "expired"
        for claim in reservation.seats:
            claim.status = "expired"
    if due:
        db.flush()
    return len(due)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/live")
def liveness():
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness(request: Request, db: Session = Depends(db_session)):
    try:
        db.execute(select(1))
        redis_client = getattr(request.app.state, "redis", None)
        if redis_client is not None:
            await redis_client.ping()
        return {"status": "ready"}
    except Exception:
        db.rollback()
        raise HTTPException(503, "A required service is unavailable")


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/auth/register", response_model=TokenOut, status_code=201)
def register(data: RegisterIn, db: Session = Depends(db_session)):
    user = User(email=data.email.lower(), password_hash=password_hash.hash(data.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An account with this email already exists")
    db.refresh(user)
    return TokenOut(access_token=make_token(user))


@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn, db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if user is None or not password_hash.verify(data.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    return TokenOut(access_token=make_token(user))


@app.get("/events", response_model=list[EventOut])
def list_events(db: Session = Depends(db_session)):
    return list(db.scalars(select(Event).where(Event.is_published.is_(True),
                                               Event.starts_at > datetime.now(timezone.utc))
                           .order_by(Event.starts_at)))


@app.get("/events/{event_id}", response_model=EventDetail)
def get_event(event_id: int, db: Session = Depends(db_session)):
    event = db.get(Event, event_id)
    if event is None or not event.is_published:
        raise HTTPException(404, "Event not found")
    expire_due(db, event_id)
    db.commit()
    return EventDetail(id=event.id, name=event.name, venue=event.venue,
                       starts_at=event.starts_at, is_published=event.is_published,
                       seats=seat_listing(db, event_id))


@app.post("/events", response_model=EventOut, status_code=201)
def create_event(data: EventCreate, _: User = Depends(admin_user), db: Session = Depends(db_session)):
    if data.starts_at.tzinfo is None or data.starts_at.utcoffset() is None:
        raise HTTPException(422, "starts_at must include a timezone")
    labels = [seat.label for seat in data.seats]
    if len(labels) != len(set(labels)):
        raise HTTPException(422, "Seat labels must be unique within an event")
    event = Event(name=data.name, venue=data.venue, starts_at=data.starts_at,
                  is_published=data.is_published,
                  seats=[Seat(label=seat.label, price_cents=seat.price_cents) for seat in data.seats])
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Event could not be created due to a data conflict")
    db.refresh(event)
    return event


@app.patch("/events/{event_id}", response_model=EventOut)
def update_event(event_id: int, data: EventUpdate, _: User = Depends(admin_user),
                 db: Session = Depends(db_session)):
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    changes = data.model_dump(exclude_unset=True)
    added_seats = changes.pop("add_seats", [])
    if any(value is None for value in changes.values()):
        raise HTTPException(422, "Event fields may not be null")
    if "starts_at" in changes:
        starts_at = changes["starts_at"]
        if starts_at is None or starts_at.tzinfo is None or starts_at.utcoffset() is None:
            raise HTTPException(422, "starts_at must include a timezone")
    existing_labels = set(db.scalars(select(Seat.label).where(Seat.event_id == event_id)))
    new_labels = [seat["label"] for seat in added_seats]
    if len(new_labels) != len(set(new_labels)) or existing_labels.intersection(new_labels):
        raise HTTPException(409, "Seat labels must be unique within an event")
    for field, value in changes.items():
        setattr(event, field, value)
    for seat in added_seats:
        event.seats.append(Seat(label=seat["label"], price_cents=seat["price_cents"]))
    db.commit()
    db.refresh(event)
    return event


@app.get("/events/{event_id}/seats", response_model=list[SeatOut])
def list_seats(event_id: int, db: Session = Depends(db_session)):
    event = db.get(Event, event_id)
    if event is None or not event.is_published:
        raise HTTPException(404, "Event not found")
    expire_due(db, event_id)
    db.commit()
    return seat_listing(db, event_id)


@app.post("/events/{event_id}/reservations", response_model=ReservationOut, status_code=201)
def hold_seats(event_id: int, data: HoldIn, idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
               user: User = Depends(current_user), db: Session = Depends(db_session)):
    seat_ids = sorted(set(data.seat_ids))
    if len(seat_ids) != len(data.seat_ids):
        raise HTTPException(422, "seat_ids must not contain duplicates")
    event = db.get(Event, event_id)
    if event is None or not event.is_published:
        raise HTTPException(404, "Event not found")
    if event.starts_at <= datetime.now(timezone.utc):
        raise HTTPException(409, "Event has already started")
    fingerprint = hashlib.sha256(json.dumps({"event_id": event_id, "seat_ids": seat_ids}, separators=(",", ":")).encode()).hexdigest()
    try:
        # Serialize retries of the same key. A hash collision only causes extra
        # serialization; PostgreSQL still validates the unique key constraint.
        db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"{user.id}:{idempotency_key}"))))
        expire_due(db, event_id)
        prior = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user.id,
                                                        IdempotencyKey.key == idempotency_key,
                                                        IdempotencyKey.operation == "hold"))
        if prior:
            if prior.request_fingerprint != fingerprint:
                raise HTTPException(409, "Idempotency-Key was already used for a different request")
            result = db.get(Reservation, prior.reservation_id)
            if result is None:
                raise HTTPException(409, "Original reservation no longer exists")
            db.commit()
            return reservation_out(result)

        seats = list(db.scalars(select(Seat).where(Seat.event_id == event_id,
                                                   Seat.id.in_(seat_ids)).order_by(Seat.id).with_for_update()))
        if len(seats) != len(seat_ids):
            raise HTTPException(404, "One or more seats do not belong to this event")
        now = datetime.now(timezone.utc)
        live = set(db.scalars(select(ReservationSeat.seat_id).join(Reservation)
                              .where(ReservationSeat.seat_id.in_(seat_ids),
                                     ReservationSeat.status.in_(["held", "confirmed"]),
                                     (ReservationSeat.status == "confirmed") |
                                     (Reservation.expires_at > now))))
        if live:
            raise HTTPException(409, "One or more seats are no longer available", headers={"Retry-After": "1"})
        reservation = Reservation(user_id=user.id, event_id=event_id, status="held",
                                  expires_at=now + timedelta(seconds=settings.hold_ttl_seconds),
                                  idempotency_key=idempotency_key)
        db.add(reservation)
        db.flush()
        reservation.seats = [ReservationSeat(seat_id=sid, status="held") for sid in seat_ids]
        db.add(IdempotencyKey(user_id=user.id, key=idempotency_key,
                              operation="hold", request_fingerprint=fingerprint, reservation_id=reservation.id))
        db.commit()
        db.refresh(reservation)
        return reservation_out(reservation)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "One or more seats are no longer available")


@app.post("/reservations/{reservation_id}/confirm", response_model=ReservationOut)
def confirm(reservation_id: int, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key",
                                                                          min_length=1, max_length=128),
            user: User = Depends(current_user), db: Session = Depends(db_session)):
    try:
        fingerprint = hashlib.sha256(f"confirm:{reservation_id}".encode()).hexdigest()
        if idempotency_key:
            db.execute(select(func.pg_advisory_xact_lock(
                func.hashtext(f"confirm:{user.id}:{idempotency_key}"))))
            prior = db.scalar(select(IdempotencyKey).where(
                IdempotencyKey.user_id == user.id,
                IdempotencyKey.key == idempotency_key,
                IdempotencyKey.operation == "confirm"))
            if prior is not None:
                if prior.request_fingerprint != fingerprint:
                    raise HTTPException(409, "Idempotency-Key was already used for a different confirmation")
                existing = db.scalar(select(Reservation).where(
                    Reservation.id == prior.reservation_id, Reservation.user_id == user.id))
                if existing is None:
                    raise HTTPException(409, "Original confirmation no longer exists")
                db.commit()
                return reservation_out(existing)

        reservation = db.scalar(select(Reservation).where(Reservation.id == reservation_id).with_for_update())
        if reservation is None or reservation.user_id != user.id:
            raise HTTPException(404, "Reservation not found")
        expire_due(db, reservation.event_id)
        db.refresh(reservation)
        if reservation.status == "confirmed":
            booking = db.scalar(select(Booking).where(Booking.reservation_id == reservation.id))
            if booking is None:
                booking = Booking(reference=uuid.uuid4().hex.upper(), reservation_id=reservation.id,
                                  user_id=reservation.user_id, event_id=reservation.event_id)
                db.add(booking)
                db.flush()
                queue_booking_notification(db, booking, user.email, "booking_confirmed")
            if idempotency_key:
                db.add(IdempotencyKey(user_id=user.id, key=idempotency_key, operation="confirm",
                                      request_fingerprint=fingerprint, reservation_id=reservation.id))
            db.commit()
            return reservation_out(reservation)
        if reservation.status == "expired":
            db.commit()
            raise HTTPException(409, "Reservation has expired or is no longer active")
        if reservation.status != "held":
            raise HTTPException(409, "Reservation has expired or is no longer active")
        if reservation.expires_at <= datetime.now(timezone.utc):
            reservation.status = "expired"
            for claim in reservation.seats:
                claim.status = "expired"
            db.commit()
            raise HTTPException(409, "Reservation has expired or is no longer active")
        reservation.status = "confirmed"
        for claim in reservation.seats:
            claim.status = "confirmed"
        booking = Booking(reference=uuid.uuid4().hex.upper(), reservation_id=reservation.id,
                          user_id=reservation.user_id, event_id=reservation.event_id, status="confirmed")
        db.add(booking)
        db.flush()
        queue_booking_notification(db, booking, user.email, "booking_confirmed")
        if idempotency_key:
            db.add(IdempotencyKey(user_id=user.id, key=idempotency_key, operation="confirm",
                                  request_fingerprint=fingerprint, reservation_id=reservation.id))
        db.commit()
        db.refresh(reservation)
        return reservation_out(reservation)
    except HTTPException:
        db.rollback()
        raise


def cancel_reservation(reservation: Reservation, db: Session) -> None:
    if reservation.status == "confirmed":
        event = db.get(Event, reservation.event_id)
        if event.starts_at <= datetime.now(timezone.utc):
            raise HTTPException(409, "Confirmed bookings cannot be cancelled after the event starts")
        booking = db.scalar(select(Booking).where(Booking.reservation_id == reservation.id))
        if booking is not None:
            if booking.status != "cancelled":
                booking.status = "cancelled"
                booking.cancelled_at = datetime.now(timezone.utc)
                user = db.get(User, reservation.user_id)
                queue_booking_notification(db, booking, user.email, "booking_cancelled")
    if reservation.status in {"held", "confirmed"}:
        reservation.status = "cancelled"
        for claim in reservation.seats:
            claim.status = "cancelled"


@app.post("/reservations/{reservation_id}/cancel", response_model=ReservationOut)
def cancel(reservation_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    reservation = db.scalar(select(Reservation).where(Reservation.id == reservation_id).with_for_update())
    if reservation is None or reservation.user_id != user.id:
        db.rollback()
        raise HTTPException(404, "Reservation not found")
    try:
        cancel_reservation(reservation, db)
        db.commit()
        db.refresh(reservation)
        return reservation_out(reservation)
    except HTTPException:
        db.rollback()
        raise


@app.get("/reservations/{reservation_id}", response_model=ReservationOut)
def get_reservation(reservation_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    reservation = db.scalar(select(Reservation).where(Reservation.id == reservation_id,
                                                     Reservation.user_id == user.id))
    if reservation is None:
        raise HTTPException(404, "Reservation not found")
    return reservation_out(reservation)


@app.get("/bookings", response_model=list[BookingOut])
def list_bookings(user: User = Depends(current_user), db: Session = Depends(db_session)):
    bookings = db.scalars(select(Booking).where(Booking.user_id == user.id)
                          .order_by(Booking.created_at.desc()))
    return [booking_out(booking) for booking in bookings]


@app.get("/bookings/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    booking = db.scalar(select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id))
    if booking is None:
        raise HTTPException(404, "Booking not found")
    return booking_out(booking)


@app.post("/bookings/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(booking_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    booking = db.scalar(select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id))
    if booking is None:
        raise HTTPException(404, "Booking not found")
    reservation = db.scalar(select(Reservation).where(Reservation.id == booking.reservation_id).with_for_update())
    try:
        cancel_reservation(reservation, db)
        db.commit()
        db.refresh(booking)
        return booking_out(booking)
    except HTTPException:
        db.rollback()
        raise


@app.post("/bookings/{booking_id}/payments", response_model=PaymentOut, status_code=201)
def simulate_payment(booking_id: int, data: PaymentIn,
                    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
                    user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Record a local payment outcome; this endpoint never contacts a payment provider."""
    booking = db.scalar(select(Booking).where(Booking.id == booking_id,
                                              Booking.user_id == user.id).with_for_update())
    if booking is None:
        raise HTTPException(404, "Booking not found")
    outcome = "succeeded" if data.outcome == "success" else "failed"
    fingerprint = hashlib.sha256(f"{booking.id}:{outcome}".encode()).hexdigest()
    prior = db.scalar(select(PaymentAttempt).where(PaymentAttempt.booking_id == booking.id,
                                                    PaymentAttempt.idempotency_key == idempotency_key))
    if prior is not None:
        if prior.request_fingerprint != fingerprint:
            raise HTTPException(409, "Idempotency-Key was already used for a different payment outcome")
        return payment_out(prior)
    if booking.status != "confirmed":
        raise HTTPException(409, "Only confirmed bookings can have simulated payments")
    if booking.payment_status == "paid":
        raise HTTPException(409, "Booking has already been paid")
    amount_cents = db.scalar(select(func.coalesce(func.sum(Seat.price_cents), 0))
                             .join(ReservationSeat, ReservationSeat.seat_id == Seat.id)
                             .where(ReservationSeat.reservation_id == booking.reservation_id))
    booking.payment_status = "pending"
    attempt = PaymentAttempt(booking_id=booking.id, idempotency_key=idempotency_key,
                             request_fingerprint=fingerprint, outcome=outcome,
                             amount_cents=amount_cents or 0)
    db.add(attempt)
    booking.payment_status = "paid" if outcome == "succeeded" else "failed"
    db.commit()
    db.refresh(attempt)
    return payment_out(attempt)


@app.get("/bookings/{booking_id}/payments", response_model=list[PaymentOut])
def list_payment_attempts(booking_id: int, user: User = Depends(current_user),
                          db: Session = Depends(db_session)):
    booking = db.scalar(select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id))
    if booking is None:
        raise HTTPException(404, "Booking not found")
    attempts = db.scalars(select(PaymentAttempt).where(PaymentAttempt.booking_id == booking.id)
                          .order_by(PaymentAttempt.created_at))
    return [payment_out(attempt) for attempt in attempts]
