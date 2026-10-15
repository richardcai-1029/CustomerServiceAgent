"""JWT access tokens."""

from datetime import UTC, datetime, timedelta

import jwt

from cs_agent.config import Settings

ALGORITHM = "HS256"


class InvalidToken(Exception):
    pass


def issue_token(settings: Settings, customer_id: int) -> str:
    issued = datetime.now(UTC)
    payload = {
        "sub": str(customer_id),
        "iat": issued,
        "exp": issued + timedelta(hours=settings.jwt_ttl_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(settings: Settings, token: str) -> int:
    """Return the customer id in a valid token, or raise InvalidToken."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidToken("Invalid or expired token") from exc
