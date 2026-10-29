"""Instance URL validation shared by the web app and the CLI (SSRF guard)."""
import ipaddress
from urllib.parse import urlparse


class InvalidInstanceURL(ValueError):
    """Raised when a Mastodon instance URL fails validation."""


def validate_instance_url(url: str) -> str:
    """Reject private/loopback URLs to prevent SSRF. Returns the normalized URL."""
    try:
        parsed = urlparse(url)
    except Exception:
        raise InvalidInstanceURL("Invalid instance URL")
    if parsed.scheme not in ("http", "https"):
        raise InvalidInstanceURL("Instance URL must use http(s)")
    host = parsed.hostname or ""
    if not host:
        raise InvalidInstanceURL("Invalid instance URL")
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local"):
        raise InvalidInstanceURL("Invalid instance URL")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise InvalidInstanceURL("Invalid instance URL")
    except ValueError:
        pass  # hostname, not an IP — fine
    return url.rstrip("/")
