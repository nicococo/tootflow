"""In-memory cache for site-wide configuration (e.g. banner text).
Loaded from DB on startup; updated immediately when admin saves changes."""

_banner: str = ""
_registration_open: bool = True


def get_banner() -> str:
    return _banner


def set_banner(text: str) -> None:
    global _banner
    _banner = text.strip()


def get_registration_open() -> bool:
    return _registration_open


def set_registration_open(value: bool) -> None:
    global _registration_open
    _registration_open = value


def load_from_db(session) -> None:
    from app.models import SiteConfig
    from sqlmodel import select
    for cfg in session.exec(select(SiteConfig)).all():
        if cfg.key == "banner_text":
            set_banner(cfg.value)
        elif cfg.key == "registration_open":
            set_registration_open(cfg.value.lower() != "false")
