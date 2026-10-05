from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    secret_key: str = "changeme"
    database_url: str = "sqlite:///./tootflow.db"

    max_posts_per_campaign: int = 32  # hard cap on total posts per campaign
    max_free_campaigns: int = 5  # max unpaid non-archived campaigns per user

    metrics_max_days: int = (
        30  # only fetch metrics for posts published within this many days
    )
    metrics_max_posts_per_user: int = (
        100  # max posts to fetch metrics for per user per run
    )

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_max_posts_per_campaign: int = 30  # cap on posts generated per AI request
    export_max_per_campaign: int = 10  # cap on export generations per campaign
    openai_max_tokens: int = 4096  # max tokens per OpenAI response

    # X.com (Twitter) OAuth 1.0a consumer credentials — empty disables X UI
    x_api_key: str = ""
    x_api_secret: str = ""

    polar_access_token: str = ""
    polar_product_id: str = ""
    polar_webhook_secret: str = ""
    polar_server: str = "sandbox"  # "sandbox" or "production"
    # Date-based API version pin (Polar-Version header). 2026-04 is the
    # contract this integration was built against; supported until ~Jan 2027.
    polar_api_version: str = "2026-04"

    app_base_url: str = "http://localhost:8000"
    app_env: str = "development"  # set to "production" in prod

    admin_secret: str = ""  # set a strong random string to enable /admin

    # Legal / Impressum details
    operator_name: str = ""
    operator_street: str = ""
    operator_city: str = ""  # e.g. "12345 Berlin"
    operator_phone: str = ""
    operator_vat_id: str = ""  # Umsatzsteuer-ID, e.g. "DE123456789"
    operator_email: str = "hello@tootflow.com"

    campaign_price: str = "5"  # minimum price, digits only, no currency symbol
    campaign_price_suggested: str = "8"  # suggested price shown at checkout
    campaign_currency: str = "EUR"  # ISO code; used in JSON-LD + ToS formal text

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
