from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field

class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"

class SeatOut(BaseModel):
    id: int
    label: str
    price_cents: int
    available: bool
    status: str

class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    venue: str
    starts_at: datetime
    is_published: bool

class SeatCreate(BaseModel):
    label: str = Field(min_length=1, max_length=32)
    price_cents: int = Field(ge=0)

class EventCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    venue: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    is_published: bool = True
    seats: list[SeatCreate] = Field(min_length=1, max_length=1000)

class EventUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    venue: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: datetime | None = None
    is_published: bool | None = None
    add_seats: list[SeatCreate] = Field(default_factory=list, max_length=1000)

class EventDetail(EventOut):
    seats: list[SeatOut]

class HoldIn(BaseModel):
    seat_ids: list[int] = Field(min_length=1, max_length=12)

class ReservationOut(BaseModel):
    id: int
    event_id: int
    status: str
    expires_at: datetime
    seat_ids: list[int]
    booking_reference: str | None = None

class BookingOut(BaseModel):
    id: int
    reference: str
    reservation_id: int
    event_id: int
    status: str
    created_at: datetime
    cancelled_at: datetime | None
    seat_ids: list[int]
    payment_status: str

class PaymentIn(BaseModel):
    outcome: Literal["success", "failure"]

class PaymentOut(BaseModel):
    id: int
    booking_id: int
    outcome: str
    amount_cents: int
    created_at: datetime
