import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from functools import lru_cache


@lru_cache()
def _get_fernet() -> Fernet:
    from app.config import get_settings
    settings = get_settings()
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt_token(token: str) -> str:
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except (InvalidToken, Exception):
        # Graceful fallback: token may be plaintext (pre-encryption migration)
        return encrypted
