from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    secret_key: str = "changeme"
    database_url: str = "sqlite:///./tootflow.db"

    max_posts_per_campaign: int = 32  # hard cap on total posts per campaign
    max_free_campaigns: int = 5  # max non-archived campaigns per user (anti-abuse cap)

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

    app_base_url: str = "http://localhost:8000"
    app_env: str = "development"  # set to "production" in prod

    admin_secret: str = ""  # set a strong random string to enable /admin

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
