from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    campaigns: list["Campaign"] = Relationship(back_populates="user")
    mastodon_account: Optional["MastodonAccount"] = Relationship(back_populates="user")
    payments: list["Payment"] = Relationship(back_populates="user")


class MastodonAccount(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    instance_url: str
    access_token: str
    username: str = ""

    user: Optional[User] = Relationship(back_populates="mastodon_account")


class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    name: str
    description: str
    template_type: str
    tone: str
    language: str = "English"
    duration_days: int = 30
    posts_per_week: int = 3
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: str = "draft_unpaid"
    payment_status: str = "unpaid"
    enabled: bool = True
    ai_posts_generated: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)

    user: Optional[User] = Relationship(back_populates="campaigns")
    posts: list["Post"] = Relationship(back_populates="campaign")
    payment: Optional["Payment"] = Relationship(back_populates="campaign")


class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    content: str
    scheduled_time: datetime
    status: str = "pending"
    mastodon_status_id: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    campaign: Optional[Campaign] = Relationship(back_populates="posts")
    metrics: Optional["Metrics"] = Relationship(back_populates="post")


class Metrics(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", unique=True)
    favourites: int = 0
    reblogs: int = 0
    replies: int = 0
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    post: Optional[Post] = Relationship(back_populates="metrics")


class PageView(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    visitor_hash: str   # SHA-256(ip + date), rotates daily — privacy-safe
    path: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Payment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    campaign_id: int = Field(foreign_key="campaign.id", unique=True)
    lemonsqueezy_order_id: str = ""
    amount: float = 0.0
    currency: str = "EUR"
    status: str = "pending"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    user: Optional[User] = Relationship(back_populates="payments")
    campaign: Optional[Campaign] = Relationship(back_populates="payment")
