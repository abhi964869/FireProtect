"""Accounts: password hashing, JWT sessions, and the current-user dependency.

Why a first-party login rather than Supabase Auth
-------------------------------------------------
Supabase Auth would work, but it puts an anon key and a second SDK in the
browser and splits identity across two systems: the auth provider owns the
user, while this database owns their devices, emergency contact and history.
Every query then needs to reconcile a Supabase UUID with a local row.

Since the data already lives in Postgres (Supabase's Postgres, when
``DATABASE_URL`` points there), keeping identity in the same database as
everything it owns means one join instead of one network hop, foreign keys that
actually enforce ownership, and no keys shipped to the client at all.

Security notes
--------------
* Passwords are bcrypt-hashed. bcrypt is intentionally slow and salts every
  digest, so identical passwords produce different hashes and offline cracking
  is expensive.
* Tokens are signed with HS256 using ``JWT_SECRET``. If that variable is not
  set, a random secret is generated at boot: sessions then die on restart,
  which is inconvenient but strictly better than shipping a default secret that
  every deployment of this code would share.
* Tokens carry only the user id and an expiry. Nothing sensitive travels in a
  JWT, because a JWT is signed, not encrypted — anyone holding one can read it.
* Login failures never say *which* half was wrong. "No such account" is an
  account-existence oracle, which is how you enumerate a user list.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import User, get_db

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"

#: bcrypt silently truncates at 72 bytes. Rejecting longer input is better than
#: accepting a password whose tail is ignored, which would make two different
#: long passwords interchangeable.
MAX_PASSWORD_BYTES = 72

MIN_PASSWORD_LENGTH = 8

_runtime_secret: str | None = None


def _secret() -> str:
    """The JWT signing key.

    Falls back to a per-process random value so a misconfigured deployment
    fails safe (everyone logged out on restart) rather than fails open
    (predictable secret, forgeable tokens).
    """
    global _runtime_secret
    configured = get_settings().jwt_secret
    if configured:
        return configured
    if _runtime_secret is None:
        _runtime_secret = secrets.token_urlsafe(48)
        logger.warning(
            "JWT_SECRET is not set - generated a temporary one. Sessions will "
            "not survive a restart. Set JWT_SECRET in the environment."
        )
    return _runtime_secret


def reset_runtime_secret() -> None:
    """Drop the generated secret. Used by tests."""
    global _runtime_secret
    _runtime_secret = None


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:MAX_PASSWORD_BYTES],
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # A malformed stored hash must not 500 the login endpoint.
        logger.warning("stored password hash is malformed")
        return False


def normalise_email(email: str) -> str:
    return email.strip().lower()


def create_token(user_id: int) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``."""
    ttl_s = int(get_settings().jwt_ttl_hours * 3600)
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_s)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM), ttl_s


def decode_token(token: str) -> int | None:
    """Return the user id, or None if the token is invalid or expired."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    subject = payload.get("sub")
    if subject is None:
        return None
    try:
        return int(subject)
    except (TypeError, ValueError):
        return None


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def get_current_user_optional(
    request: Request, session: Annotated[Session, Depends(get_db)]
) -> User | None:
    """Resolve the caller, or None when unauthenticated.

    Used by endpoints that work for anonymous visitors but do more when signed
    in — the dashboard is readable by anyone with the link, but only an owner
    sees their emergency settings.
    """
    token = _bearer_token(request)
    if token is None:
        return None
    user_id = decode_token(token)
    if user_id is None:
        return None
    return session.get(User, user_id)


def get_current_user(
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Require a signed-in caller."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def find_user_by_email(session: Session, email: str) -> User | None:
    return session.scalars(
        select(User).where(User.email == normalise_email(email))
    ).first()


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user_optional)]
