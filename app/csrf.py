import hmac
import secrets

from fastapi import Form, Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

CSRF_COOKIE = "csrftoken"


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection.

    Sets a csrftoken cookie on every response. Route handlers read the token
    from request.state.csrf_token to embed it in forms. The verify_csrf
    dependency validates the submitted form field matches the cookie.
    """

    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        request.state.csrf_token = token
        response = await call_next(request)
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(
                CSRF_COOKIE,
                token,
                httponly=False,   # Must be readable server-side via request.state
                samesite="strict",
                path="/",
            )
        return response


async def verify_csrf(request: Request, csrf_token: str = Form(default="")):
    """FastAPI dependency. Add to any POST route that processes an HTML form."""
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if not cookie_token or not hmac.compare_digest(csrf_token, cookie_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
