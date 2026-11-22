from datetime import datetime
from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
                        JSON, String, UniqueConstraint, func, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    venue: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    seats: Mapped[list["Seat"]] = relationship(back_populates="event", cascade="all, delete-orphan")

class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (UniqueConstraint("event_id", "label", name="uq_seat_event_label"),
                      CheckConstraint("price_cents >= 0", name="ck_seat_nonnegative_price"))
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(32))
    price_cents: Mapped[int] = mapped_column(Integer)
    event: Mapped[Event] = relationship(back_populates="seats")

class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (CheckConstraint("status IN ('held','confirmed','cancelled','expired')", name="ck_reservation_status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="held")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    seats: Mapped[list["ReservationSeat"]] = relationship(cascade="all, delete-orphan")
    booking: Mapped["Booking | None"] = relationship(back_populates="reservation", uselist=False)

class ReservationSeat(Base):
    __tablename__ = "reservation_seats"
    __table_args__ = (
        UniqueConstraint("reservation_id", "seat_id", name="uq_reservation_seat"),
        CheckConstraint("status IN ('held','confirmed','cancelled','expired')", name="ck_reservation_seat_status"),
        Index("uq_live_seat_claim", "seat_id", unique=True, postgresql_where=text("status IN ('held','confirmed')")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), index=True)
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="held")

class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "key", "operation", name="uq_idempotency_user_key_operation"),
        CheckConstraint("operation IN ('hold','confirm')", name="ck_idempotency_operation"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(16), server_default="hold")
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"))

class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("status IN ('confirmed','cancelled')", name="ck_booking_status"),
        CheckConstraint("payment_status IN ('not_required','pending','paid','failed')", name="ck_booking_payment_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="confirmed")
    payment_status: Mapped[str] = mapped_column(String(16), server_default="not_required")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reservation: Mapped[Reservation] = relationship(back_populates="booking")

class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint("booking_id", "idempotency_key", name="uq_payment_booking_idempotency"),
        CheckConstraint("outcome IN ('succeeded','failed')", name="ck_payment_outcome"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))
    amount_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (CheckConstraint("status IN ('queued','simulated')", name="ck_notification_status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    recipient_email: Mapped[str] = mapped_column(String(320))
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
