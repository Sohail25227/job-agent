"""Multi-user authentication.

Accounts live in the database as a scrypt hash with a per-user salt; the
plaintext password is never stored or logged. No third-party auth service:
hashlib and a signed session cookie are enough, and they cost nothing to run.

The session key comes from ``SECRET_KEY``. On a host with an ephemeral
filesystem a generated file would be a new key after every deploy, silently
logging everyone out, so a real environment variable is the only correct answer
in production - see ``secret_key`` for the local fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import PROJECT_ROOT
from ..models import User, utcnow

log = logging.getLogger(__name__)

# Interactive-login parameters: ~100ms per attempt. Deliberately slow.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
# scrypt needs 128 * N * r bytes, which is exactly 32 MB here - and OpenSSL's
# default ceiling is also 32 MB, so it refuses with "memory limit exceeded"
# unless the budget is raised explicitly.
SCRYPT_MAXMEM = 96 * 1024 * 1024

MIN_PASSWORD_LENGTH = 8
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)

KEY_FILE = PROJECT_ROOT / "data" / "session.key"


def _derive(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=64,
    ).hex()


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def create_user(session: Session, email: str, password: str) -> User:
    """Register an account. Raises ValueError with a message fit for the UI."""
    email = normalise_email(email)
    if not EMAIL.match(email):
        raise ValueError("That does not look like an email address.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if session.scalar(select(User).where(User.email == email)):
        raise ValueError("An account with that email already exists.")

    salt = secrets.token_bytes(16)
    user = User(
        email=email,
        salt=salt.hex(),
        password_hash=_derive(password, salt),
        last_login_at=utcnow(),
    )
    session.add(user)
    session.flush()
    log.info("account created: %s", email)
    return user


def verify(session: Session, email: str, password: str) -> User | None:
    """Return the user on a correct password, else None."""
    user = session.scalar(select(User).where(User.email == normalise_email(email)))
    if user is None:
        # Spend the same time as a real check so a missing account is not
        # distinguishable by how fast the answer comes back.
        _derive(password or "", b"absent-user-salt")
        return None
    if not hmac.compare_digest(user.password_hash, _derive(password or "", bytes.fromhex(user.salt))):
        return None
    user.last_login_at = utcnow()
    return user


def change_password(session: Session, user: User, old: str, new: str) -> None:
    if not hmac.compare_digest(user.password_hash, _derive(old or "", bytes.fromhex(user.salt))):
        raise ValueError("Current password is not correct.")
    if len(new or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    user.salt = salt.hex()
    user.password_hash = _derive(new, salt)


def user_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(User)) or 0


def claim_search(user: User, daily_limit: int) -> bool:
    """Count one live search against the user's daily allowance.

    Live searches hit third-party APIs on a key shared by every account, so the
    allowance is what stops one user from spending the whole day's quota.
    Returns False when they are out.
    """
    today = date.today()
    if user.searches_on != today:
        user.searches_on = today
        user.searches_today = 0
    if daily_limit and user.searches_today >= daily_limit:
        return False
    user.searches_today += 1
    return True


def secret_key() -> str:
    """Session-signing key: the environment first, a local file as a fallback.

    If the filesystem is not writable the key is only held in memory, which
    means sessions do not survive a restart. That is the correct failure for a
    container: annoying locally, harmless in production where ``SECRET_KEY`` is
    set anyway.
    """
    key = os.getenv("SECRET_KEY")
    if key:
        return key
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()

    key = secrets.token_urlsafe(48)
    try:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key)
        KEY_FILE.chmod(0o600)
        where = f"generated one in {KEY_FILE}"
    except OSError:
        where = "generated a temporary one; sessions end when this process does"
    log.warning(
        "SECRET_KEY is not set; %s. Set the environment variable before "
        "deploying, or every deploy logs all users out.", where,
    )
    return key
