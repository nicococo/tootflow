"""In-memory cache for site-wide configuration (e.g. banner text).
Loaded from DB on startup; updated immediately when admin saves changes."""

_banner: str = ""


def get_banner() -> str:
    return _banner


def set_banner(text: str) -> None:
    global _banner
    _banner = text.strip()


def load_from_db(session) -> None:
    from app.models import SiteConfig
    from sqlmodel import select
    cfg = session.exec(select(SiteConfig).where(SiteConfig.key == "banner_text")).first()
    if cfg:
        set_banner(cfg.value)
