# Ticket Booking Service

FastAPI + PostgreSQL backend for public event discovery, seat selection, temporary
holds, confirmation, cancellation, expiry, and idempotent reservation creation.

## Implemented capabilities

- Email/password registration and bearer-token authentication
- Event discovery and administrator-managed event and seat inventory
- Temporary seat holds with configurable expiry
- Reservation confirmation, cancellation, and booking history
- Idempotency keys for hold and confirmation retries
- PostgreSQL row locking and a partial unique index to prevent double booking
- Background expiry processing using `FOR UPDATE SKIP LOCKED`
- Redis-backed write-request rate limiting and upcoming-event caching
- Request IDs, structured logs, health probes, and Prometheus metrics
- Simulated payment attempts and a transactional notification outbox
- Alembic migrations, Docker Compose, automated tests, and GitHub Actions CI

Redis is not a booking source of truth: PostgreSQL owns seat claims and expiry.
Redis is used for distributed write-request rate limits, where shared counters
between API replicas are useful. The upcoming event list is cached for 30 seconds;
event writes invalidate it. Seat availability is never cached.

## Run locally

```sh
cp .env.example .env
docker compose up --build -d
docker compose exec api python scripts/seed.py
docker compose exec api python scripts/promote_admin.py your-email@example.com
```

API docs: http://localhost:8000/docs. The sample event has 50 seats. Register the
administrator account first, then promote it with the command above. Registration
returns a bearer token; send it as `Authorization: Bearer <token>`.

For local tests, install development dependencies with `pip install -e '.[dev]'`,
start PostgreSQL with `docker compose up -d db`, set `DATABASE_URL` in `.env` to
`postgresql+psycopg://tickets:tickets@localhost:5432/tickets`, run
`alembic upgrade head`, and then run `pytest`. Tests truncate data in the configured
database, so use a dedicated test database. GitHub Actions runs migrations, lint,
and the PostgreSQL test suite on pushes and pull requests.

## API outline

- `POST /auth/register`, `POST /auth/login`
- `POST /events`, `PATCH /events/{event_id}` (administrator only)
- `GET /events`, `GET /events/{event_id}`, `GET /events/{event_id}/seats`
- `POST /events/{event_id}/reservations` with `Idempotency-Key` and `seat_ids`
- `GET /reservations/{id}`, `POST /reservations/{id}/confirm`, `POST /reservations/{id}/cancel`
- `GET /bookings`, `GET /bookings/{id}`, `POST /bookings/{id}/cancel`
- `POST /bookings/{id}/payments`, `GET /bookings/{id}/payments` (simulated only)
- `GET /health/live`, `GET /health/ready`, `GET /metrics`

Event creation defines the initial seat inventory. Event updates can append seats
with `add_seats`; existing seat labels and prices are kept stable after creation.
Seat availability reports `available`, `held`, or `booked`. Confirmation creates
a separate booking with a unique reference. Booking owners can view and cancel
their own bookings until the event start time; cancellations release the seats.
The optional simulated payment endpoint records success/failure attempts and
permits retries with a new idempotency key. It never contacts a payment provider
and does not change seat ownership. Confirmation/cancellation notifications are
written transactionally to a database outbox; the worker marks them simulated and
logs each event. Connect a notification provider before treating these as delivered.

Holds last `HOLD_TTL_SECONDS` (default 600). Use the returned reservation ID to
confirm or cancel. Repeating the same hold request with the same key returns the
original reservation; reusing that key with a different payload returns 409.
Confirmation also accepts `Idempotency-Key`; retries return the same confirmed
reservation and booking reference. Hold and confirmation keys use separate scopes.
Expired holds are cleaned up on seat reads and hold creation, and the worker polls
every two seconds for unattended expirations. A confirmed reservation remains
exclusive until cancellation. New holds and confirmations are rejected after the event
starts; outstanding holds are expired at the event start time.

## Concurrency design

Hold creation locks requested seat rows in ascending ID order, which reduces
deadlocks for multi-seat requests. It checks live claims inside the transaction.
PostgreSQL's `uq_live_seat_claim` partial unique index on
`reservation_seats(seat_id)` for `status IN ('held','confirmed')` is the final
integrity guard even if an application path omits the pre-check. Conflicts return
HTTP 409. Confirmation and cancellation lock the reservation row, so those
transitions cannot race each other. The worker uses `FOR UPDATE SKIP LOCKED`, so
multiple worker replicas can process expiry without claiming the same reservation.

## Tests and load measurement

`tests/test_booking.py` races authenticated users for the same seat and for
different seats, then checks confirmation/payment idempotency, cancellation,
expiry cleanup, authentication, transaction rollback, event-cache invalidation,
request telemetry, and notification outbox processing.

To measure API contention against a live seeded service:

```bash
EVENT_ID=1 SEAT_ID=1 REQUESTS=50 python scripts/load_test.py
```

Choose an available seat; the successful request holds it until expiry or
cancellation. The script reports wall-clock elapsed time, request throughput, and status counts.
**Measured results:** run the command in the target environment and record its
output here. No load measurement is claimed until that command has been run.

## Current scope

Authentication is email/password with signed bearer tokens. Mutating API requests
are rate-limited by client IP through Redis (120/minute by default); rate-limit
counters are shared by API replicas, and write requests fail closed if configured
Redis is unavailable. Request IDs are returned in `X-Request-ID` and structured
logs include method, route, status, and duration. Prometheus metrics are process
local, so aggregate them across API replicas in your monitoring system. There is
no actual payment provider or email delivery. Use migrations for schema changes;
`create_all` is enabled only for local/test startup.


## Configuration

Copy `.env.example` to `.env` before starting the stack. Set `JWT_SECRET` to a
long random value and keep `.env` out of version control. Docker Compose overrides
`DATABASE_URL` and `REDIS_URL` for its internal service hostnames; for tests run
from your host machine, use the `localhost` PostgreSQL URL shown above.

Important settings:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection URL |
| `JWT_SECRET` | Secret used to sign bearer tokens |
| `ACCESS_TOKEN_MINUTES` | Access-token lifetime |
| `HOLD_TTL_SECONDS` | Duration of a temporary seat hold |
| `AUTO_CREATE_SCHEMA` | Optional local/test schema creation; migrations are used in deployment |
| `REDIS_URL` | Optional Redis cache and rate-limit connection |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | Mutating-request limit per client IP |
| `EVENT_CACHE_TTL_SECONDS` | Upcoming-event list cache lifetime |

## Project structure

```text
app/
  config.py             # Environment-backed settings
  database.py           # SQLAlchemy engine and session factory
  main.py               # FastAPI routes and request middleware
  models.py             # SQLAlchemy models and constraints
  schemas.py            # API request/response schemas
  expiry_worker.py      # Reservation expiry and outbox worker
  notifications.py      # Transactional outbox processing
migrations/             # Alembic schema revisions
scripts/                # Seed, admin and contention-test utilities
tests/                  # PostgreSQL integration tests
.github/workflows/      # CI pipeline
```

The API routes currently live in `app/main.py`. Splitting the route handlers into
routers and extracting domain operations into services is a sensible next
refactoring, but it is intentionally not represented as completed work here.
