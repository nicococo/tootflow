from datetime import datetime, timedelta
from typing import Optional
from fastapi import Request, HTTPException, Depends
from sqlmodel import Session
from jose import JWTError, jwt

from app.config import get_settings
from app.database import get_session
from app.models import User

settings = get_settings()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


def create_access_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": str(user_id), "exp": expire}, settings.secret_key, algorithm=ALGORITHM)


def get_token_payload(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except (JWTError, TypeError):
        return None


def auth_cookie_params() -> dict:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.app_env == "production",
        "max_age": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    payload = get_token_payload(token)
    if not payload:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    user = session.get(User, int(payload.get("sub", 0)))
    if not user:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user


def optional_current_user(request: Request, session: Session = Depends(get_session)) -> Optional[User]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = get_token_payload(token)
    if not payload:
        return None
    return session.get(User, int(payload.get("sub", 0)))
