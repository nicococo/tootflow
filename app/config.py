from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    secret_key: str = "changeme"
    database_url: str = "sqlite:///./tootflow.db"

    max_posts_per_campaign: int = 32          # hard cap on total posts per campaign

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_max_posts_per_campaign: int = 30   # cap on posts generated per AI request
    export_max_per_campaign: int = 5          # cap on export generations per campaign
    openai_max_tokens: int = 4096             # max tokens per OpenAI response

    lemonsqueezy_api_key: str = ""
    lemonsqueezy_store_id: str = ""
    lemonsqueezy_product_variant_id: str = ""
    lemonsqueezy_webhook_secret: str = ""

    app_base_url: str = "http://localhost:8000"
    app_env: str = "development"  # set to "production" in prod

    admin_secret: str = ""  # set a strong random string to enable /admin

    # Legal / Impressum details
    operator_name: str = ""
    operator_street: str = ""
    operator_city: str = ""          # e.g. "12345 Berlin"
    operator_phone: str = ""
    operator_vat_id: str = ""        # e.g. "DE123456789" or "Kleinunternehmer per §19 UStG"
    operator_email: str = "hello@tootflow.com"

    campaign_price: str = "5"         # digits only, no currency symbol
    campaign_currency: str = "EUR"    # ISO code; used in JSON-LD + ToS formal text

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
