"""Promote an existing account: python scripts/promote_admin.py user@example.com"""
import sys
from sqlalchemy import select
from app.database import SessionLocal
from app.models import User

if len(sys.argv) != 2:
    raise SystemExit("usage: python scripts/promote_admin.py EMAIL")

with SessionLocal.begin() as db:
    user = db.scalar(select(User).where(User.email == sys.argv[1].lower()))
    if user is None:
        raise SystemExit("account not found; register the account first")
    user.is_admin = True
    print(f"Granted administrator access to {user.email}")
