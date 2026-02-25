import re
from datetime import datetime, timedelta
from typing import List
from openai import OpenAI
from app.config import get_settings

settings = get_settings()


TEMPLATE_DESCRIPTIONS = {
    "product_launch": "a product launch campaign to generate buzz and drive adoption",
    "open_source_release": "an open source software release campaign for the developer community",
    "event_promotion": "an event promotion campaign to drive registrations and attendance",
}

TONE_DESCRIPTIONS = {
    "professional": "professional and authoritative",
    "casual": "friendly and conversational",
    "technical": "technical and detailed, for a developer audience",
    "inspiring": "motivational and inspiring",
}


def _sanitize(text: str, max_len: int) -> str:
    """Strip control characters and limit length to reduce prompt injection risk (Fix #11)."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)  # strip control chars
    return text[:max_len].strip()


def generate_posts(
    campaign_name: str,
    description: str,
    template_type: str,
    tone: str,
    language: str,
    duration_days: int,
    posts_per_week: int,
) -> List[str]:
    client = OpenAI(api_key=settings.openai_api_key)

    total_posts = min(
        max(1, int(duration_days / 7 * posts_per_week)),
        settings.openai_max_posts_per_campaign,
    )
    template_desc = TEMPLATE_DESCRIPTIONS.get(template_type, template_type)
    tone_desc = TONE_DESCRIPTIONS.get(tone, tone)

    # Sanitize user-supplied inputs before inserting into prompt
    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)

    # Use system/user role separation to isolate instructions from user data (Fix #11)
    system_prompt = (
        f"You are a social media expert creating Mastodon posts for {template_desc}. "
        f"Generate exactly {total_posts} posts. Each post must: "
        f"be under 500 characters, match the {tone_desc} tone, "
        f"feel natural for Mastodon (not Twitter-style), and optionally include 1-3 relevant hashtags. "
        f"Output ONLY the posts, separated by a line containing exactly '---'. "
        f"Do not number them or add any explanation."
    )

    user_prompt = (
        f"Campaign name: {safe_name}\n"
        f"Description: {safe_desc}\n"
        f"Language: {safe_lang}\n"
        f"Total posts: {total_posts}"
    )

    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.8,
        max_tokens=settings.openai_max_tokens,
    )

    raw = response.choices[0].message.content.strip()
    posts = [p.strip() for p in raw.split("---") if p.strip()]
    posts = [p[:500] for p in posts]
    return posts[:total_posts]


def regenerate_single_post(
    campaign_name: str,
    description: str,
    template_type: str,
    tone: str,
    language: str,
) -> str:
    """Generate a single replacement post in the context of the campaign."""
    client = OpenAI(api_key=settings.openai_api_key)

    template_desc = TEMPLATE_DESCRIPTIONS.get(template_type, template_type)
    tone_desc = TONE_DESCRIPTIONS.get(tone, tone)

    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)

    system_prompt = (
        f"You are a social media expert writing a single Mastodon post for {template_desc}. "
        f"The post must: be under 500 characters, match the {tone_desc} tone, "
        f"feel natural for Mastodon (not Twitter-style), and optionally include 1-3 relevant hashtags. "
        f"Output ONLY the post text. No explanation, no numbering, no quotes."
    )

    user_prompt = (
        f"Campaign name: {safe_name}\n"
        f"Description: {safe_desc}\n"
        f"Language: {safe_lang}"
    )

    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.9,
        max_tokens=min(settings.openai_max_tokens, 600),  # single post never needs more
    )

    return response.choices[0].message.content.strip()[:500]


def schedule_posts(posts: List[str], start_date: datetime, posts_per_week: int) -> List[tuple]:
    """Return list of (content, scheduled_time) tuples spread across the campaign."""
    scheduled = []
    interval_days = 7 / max(1, posts_per_week)
    for i, content in enumerate(posts):
        offset = timedelta(days=i * interval_days + 1)
        scheduled_time = start_date + offset
        scheduled_time = scheduled_time.replace(hour=10, minute=0, second=0, microsecond=0)
        scheduled.append((content, scheduled_time))
    return scheduled
