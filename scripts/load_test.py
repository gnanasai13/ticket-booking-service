"""Small real HTTP contention test. Start the API, seed the DB, then run this script."""
import asyncio
import os
import time
import httpx

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
EVENT_ID = int(os.environ["EVENT_ID"])
SEAT_ID = int(os.environ["SEAT_ID"])
REQUESTS = int(os.getenv("REQUESTS", "50"))

async def main():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        registrations = await asyncio.gather(*[
            client.post("/auth/register", json={"email": f"load-{i}-{time.time_ns()}@example.com",
                                                 "password": "load-test-password-123"}) for i in range(REQUESTS)])
        tokens = [r.json()["access_token"] for r in registrations]
        started = time.perf_counter()
        responses = await asyncio.gather(*[
            client.post(f"/events/{EVENT_ID}/reservations",
                        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"load-{i}"},
                        json={"seat_ids": [SEAT_ID]}) for i, token in enumerate(tokens)])
        elapsed = time.perf_counter() - started
    codes = [r.status_code for r in responses]
    print(f"requests={REQUESTS} elapsed_s={elapsed:.3f} throughput_rps={REQUESTS/elapsed:.1f}")
    print(f"201={codes.count(201)} 409={codes.count(409)} other={len(codes)-codes.count(201)-codes.count(409)}")
    if codes.count(201) != 1 or codes.count(409) != REQUESTS - 1:
        raise SystemExit("contention invariant failed")

if __name__ == "__main__":
    asyncio.run(main())
