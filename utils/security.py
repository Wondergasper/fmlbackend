"""Local JWT and password utilities for offline auth/testing fallback."""

from datetime import datetime, timedelta, timezone

try:
    from jose import jwt, JWTError
    from passlib.context import CryptContext
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

JWT_SECRET = "change-me-in-production"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 1440


def hash_password(password: str) -> str:
    if not HAS_CRYPTO:
        raise ImportError("Install python-jose[cryptography] and passlib[bcrypt] for local auth")
    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if not HAS_CRYPTO:
        raise ImportError("Install python-jose[cryptography] and passlib[bcrypt] for local auth")
    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return ctx.verify(plain, hashed)


def create_access_token(data: dict, secret: str = JWT_SECRET) -> str:
    if not HAS_CRYPTO:
        raise ImportError("Install python-jose[cryptography] for local JWT support")
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str = JWT_SECRET) -> dict | None:
    if not HAS_CRYPTO:
        raise ImportError("Install python-jose[cryptography] for local JWT support")
    try:
        return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
