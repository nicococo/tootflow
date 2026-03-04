import random
import re
from datetime import datetime, timedelta
from typing import List
from openai import AsyncOpenAI
from app.config import get_settings

settings = get_settings()


TEMPLATE_DESCRIPTIONS = {
    "product_launch": "a product launch campaign to generate buzz and drive adoption",
    "open_source_release": "an open source software release campaign for the developer community",
    "event_promotion": "an event promotion campaign to drive registrations and attendance",
    "knowledge_sharing": "a knowledge-sharing campaign that drops useful insights and expertise on a specific topic to attract and grow an interested audience",
    "developer_tips": "a developer tips campaign sharing practical coding tips, tricks, and workflow improvements for a technical audience",
    "research_insights": "a research insights campaign sharing findings, data, and takeaways from academic or professional research to inform and engage a curious audience",
    "personal_brand": "a personal brand campaign that establishes thought leadership and builds a recognisable voice and presence around a person's expertise",
    "community_building": "a community building campaign that invites people to join a shared interest, sparks conversations, and grows an engaged following around a topic",
    "project_showcase": "a project showcase campaign that highlights the story, progress, and interesting details behind a creative or technical side project",
    "newsletter_growth": "a newsletter growth campaign that teases valuable content and gives compelling reasons to subscribe to a newsletter or mailing list",
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


async def generate_posts(
    campaign_name: str,
    description: str,
    template_type: str,
    tone: str,
    language: str,
    duration_days: int,
    posts_per_week: int,
) -> List[str]:
    client = AsyncOpenAI(api_key=settings.openai_api_key)

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

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.8,
        max_completion_tokens=settings.openai_max_tokens,
    )

    raw = response.choices[0].message.content.strip()
    posts = [p.strip() for p in raw.split("---") if p.strip()]
    posts = [p[:500] for p in posts]
    return posts[:total_posts]


async def regenerate_single_post(
    campaign_name: str,
    description: str,
    template_type: str,
    tone: str,
    language: str,
) -> str:
    """Generate a single replacement post in the context of the campaign."""
    client = AsyncOpenAI(api_key=settings.openai_api_key)

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

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.9,
        max_completion_tokens=min(settings.openai_max_tokens, 600),  # single post never needs more
    )

    return response.choices[0].message.content.strip()[:500]


async def refine_single_post(
    current_content: str,
    instructions: str,
    campaign_name: str,
    description: str,
    template_type: str,
    tone: str,
    language: str,
) -> str:
    """Refine an existing post based on user instructions, keeping campaign context."""
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    template_desc = TEMPLATE_DESCRIPTIONS.get(template_type, template_type)
    tone_desc = TONE_DESCRIPTIONS.get(tone, tone)

    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)
    safe_content = _sanitize(current_content, 500)
    safe_instructions = _sanitize(instructions, 300)

    system_prompt = (
        f"You are a social media expert refining a Mastodon post for {template_desc}. "
        f"Rewrite the provided post according to the user's instructions. "
        f"The refined post must: be under 500 characters, match the {tone_desc} tone, "
        f"feel natural for Mastodon (not Twitter-style), and optionally include 1-3 relevant hashtags. "
        f"Output ONLY the refined post text. No explanation, no numbering, no quotes."
    )

    user_prompt = (
        f"Campaign: {safe_name}\n"
        f"Topic: {safe_desc}\n"
        f"Language: {safe_lang}\n\n"
        f"Current post:\n{safe_content}\n\n"
        f"Instructions: {safe_instructions}"
    )

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_completion_tokens=min(settings.openai_max_tokens, 600),
    )

    return response.choices[0].message.content.strip()[:500]


async def generate_newsletter_digest(
    campaign_name: str,
    description: str,
    tone: str,
    language: str,
    post_contents: List[str],
) -> str:
    """Generate a newsletter digest from a campaign's published/pending posts."""
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)
    tone_desc = TONE_DESCRIPTIONS.get(tone, tone)
    safe_posts = "\n\n".join(_sanitize(p, 500) for p in post_contents[:40])

    system_prompt = (
        f"You are a newsletter writer. Write a cohesive newsletter digest (400–700 words) "
        f"based on a Mastodon campaign's posts. "
        f"Start with a suggested subject line prefixed 'Subject: '. "
        f"Then write the newsletter body: an engaging intro, the key insights woven into flowing prose, "
        f"and a brief call-to-action at the end. "
        f"Tone: {tone_desc}. Language: {safe_lang}. "
        f"Output only the newsletter content — no meta-commentary."
    )
    user_prompt = (
        f"Campaign: {safe_name}\n"
        f"Topic: {safe_desc}\n\n"
        f"Posts:\n{safe_posts}"
    )

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_completion_tokens=1200,
    )
    return response.choices[0].message.content.strip()


async def generate_linkedin_summary(
    campaign_name: str,
    description: str,
    tone: str,
    language: str,
    post_contents: List[str],
) -> str:
    """Generate a standalone long-form LinkedIn article from campaign insights."""
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)
    tone_desc = TONE_DESCRIPTIONS.get(tone, tone)
    safe_posts = "\n\n".join(_sanitize(p, 500) for p in post_contents[:40])

    system_prompt = (
        f"You are an expert LinkedIn content writer. "
        f"Using the provided source material as inspiration, write a compelling, standalone LinkedIn article "
        f"on the given topic. "
        f"Requirements: "
        f"Output a suggested headline on the first line, prefixed with 'Headline: '. "
        f"Then write the body as 1–2 focused paragraphs. "
        f"Condense the entire campaign into its single most important insight or argument — "
        f"punchy, self-contained, and immediately useful to the reader. "
        f"End with a concrete takeaway or call-to-action. "
        f"Write as original expert content — never mention that this was derived from social media posts "
        f"or any other platform. "
        f"Tone: {tone_desc}. Language: {safe_lang}. "
        f"Output only the headline line and article body — no meta-commentary."
    )
    user_prompt = (
        f"Topic: {safe_name} — {safe_desc}\n\n"
        f"Key insights and points to incorporate:\n{safe_posts}"
    )

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_completion_tokens=400,
    )
    return response.choices[0].message.content.strip()


def schedule_posts(posts: List[str], start_date: datetime, posts_per_week: int) -> List[tuple]:
    """Return list of (content, scheduled_time) tuples spread across the campaign."""
    scheduled = []
    interval_days = 7 / max(1, posts_per_week)
    for i, content in enumerate(posts):
        offset = timedelta(days=i * interval_days + 1)
        scheduled_time = start_date + offset
        jitter = random.randint(-18, 18)
        scheduled_time = scheduled_time.replace(hour=10, minute=0, second=0, microsecond=0)
        scheduled_time += timedelta(minutes=jitter)
        scheduled.append((content, scheduled_time))
    return scheduled
