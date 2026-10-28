from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    campaigns: list["Campaign"] = Relationship(back_populates="user")
    mastodon_account: Optional["MastodonAccount"] = Relationship(back_populates="user")


class MastodonAccount(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    instance_url: str
    access_token: str
    username: str = ""

    user: Optional[User] = Relationship(back_populates="mastodon_account")


class UserAIConfig(SQLModel, table=True):
    """Per-user AI provider credentials (bring-your-own-key).

    api_key is stored encrypted (Fernet). base_url empty = provider default;
    model empty = instance default. Works with any OpenAI-compatible endpoint
    (OpenAI, OpenRouter, Ollama, ...).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True)
    api_key: str = ""          # encrypted at rest
    base_url: str = ""         # e.g. https://openrouter.ai/api/v1
    model: str = ""            # e.g. openai/gpt-4o-mini, llama3, ...
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    mastodon_account_id: Optional[int] = Field(default=None, foreign_key="mastodonaccount.id")
    name: str
    description: str
    template_type: str
    tone: str
    language: str = "English"
    duration_days: int = 30
    posts_per_week: int = 3
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: str = "draft"
    enabled: bool = True
    ai_posts_generated: int = 0
    export_generations: int = 0
    export_drafts: str = Field(default="{}")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    user: Optional[User] = Relationship(back_populates="campaigns")
    posts: list["Post"] = Relationship(back_populates="campaign")


class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    content: str
    scheduled_time: datetime
    status: str = "pending"
    mastodon_status_id: Optional[str] = None
    published_at: Optional[datetime] = None
    poll_options: Optional[str] = None   # JSON list e.g. '["Yes","No","Maybe"]'
    poll_expires_in: int = 86400         # seconds; default = 1 day
    created_at: datetime = Field(default_factory=datetime.utcnow)

    campaign: Optional[Campaign] = Relationship(back_populates="posts")
    metrics: Optional["Metrics"] = Relationship(back_populates="post")


class Metrics(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", unique=True)
    favourites: int = 0
    reblogs: int = 0
    replies: int = 0
    poll_voters: int = 0
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    post: Optional[Post] = Relationship(back_populates="metrics")


class FollowerSnapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    count: int
    recorded_at: datetime = Field(default_factory=datetime.utcnow)


class PageView(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    visitor_hash: str   # SHA-256(ip + date), rotates daily — privacy-safe
    path: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class LoginEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AccountLink(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    linked_user_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SiteConfig(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
