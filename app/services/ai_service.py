import random
import re
from datetime import datetime, timedelta
from typing import List
import httpx
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


_PLATFORM_LIMITS = {"mastodon": 500, "x": 280}


def _platform_style(platform: str) -> tuple[int, str]:
    """Return (max_chars, style note) for a platform."""
    if platform == "x":
        return 280, "feel natural for X/Twitter (concise, punchy, no threads)"
    return 500, "feel natural for Mastodon (not Twitter-style)"


async def generate_posts(
    campaign_name: str,
    description: str,
    template_type: str,
    tone: str,
    language: str,
    duration_days: int,
    posts_per_week: int,
    platform: str = "mastodon",
) -> List[str]:
    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=httpx.AsyncClient())

    total_posts = min(
        max(1, int(duration_days / 7 * posts_per_week)),
        settings.openai_max_posts_per_campaign,
    )
    template_desc = TEMPLATE_DESCRIPTIONS.get(template_type, template_type)
    tone_desc = TONE_DESCRIPTIONS.get(tone, tone)
    max_chars, style_note = _platform_style(platform)

    # Sanitize user-supplied inputs before inserting into prompt
    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)

    # Use system/user role separation to isolate instructions from user data (Fix #11)
    system_prompt = (
        f"You are a social media expert creating social posts for {template_desc}. "
        f"Generate exactly {total_posts} posts. Each post must: "
        f"be under {max_chars} characters, match the {tone_desc} tone, "
        f"{style_note}, and optionally include 1-3 relevant hashtags. "
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
    posts = [p[:max_chars] for p in posts]
    return posts[:total_posts]


async def regenerate_single_post(
    campaign_name: str,
    description: str,
    template_type: str,
    tone: str,
    language: str,
    platform: str = "mastodon",
) -> str:
    """Generate a single replacement post in the context of the campaign."""
    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=httpx.AsyncClient())

    template_desc = TEMPLATE_DESCRIPTIONS.get(template_type, template_type)
    tone_desc = TONE_DESCRIPTIONS.get(tone, tone)
    max_chars, style_note = _platform_style(platform)

    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)

    system_prompt = (
        f"You are a social media expert writing a single social post for {template_desc}. "
        f"The post must: be under {max_chars} characters, match the {tone_desc} tone, "
        f"{style_note}, and optionally include 1-3 relevant hashtags. "
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

    return response.choices[0].message.content.strip()[:max_chars]


async def refine_single_post(
    current_content: str,
    instructions: str,
    campaign_name: str,
    description: str,
    template_type: str,
    tone: str,
    language: str,
    poll_options: list[str] | None = None,
    platform: str = "mastodon",
) -> str:
    """Refine an existing post based on user instructions, keeping campaign context."""
    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=httpx.AsyncClient())

    template_desc = TEMPLATE_DESCRIPTIONS.get(template_type, template_type)
    tone_desc = TONE_DESCRIPTIONS.get(tone, tone)
    max_chars, style_note = _platform_style(platform)

    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)
    safe_content = _sanitize(current_content, max_chars)
    safe_instructions = _sanitize(instructions, 300)

    poll_note = ""
    if poll_options and len(poll_options) >= 2:
        safe_opts = [_sanitize(o, 50) for o in poll_options[:4]]
        poll_note = (
            f"\n\nThis post has an attached poll with these options: "
            + ", ".join(f'"{o}"' for o in safe_opts)
            + ". Refine the post text to complement the poll — do not modify the poll options themselves."
        )

    system_prompt = (
        f"You are a social media expert refining a social post for {template_desc}. "
        f"Rewrite the provided post according to the user's instructions. "
        f"The refined post must: be under {max_chars} characters, match the {tone_desc} tone, "
        f"{style_note}, and optionally include 1-3 relevant hashtags. "
        f"Output ONLY the refined post text. No explanation, no numbering, no quotes."
    )

    user_prompt = (
        f"Campaign: {safe_name}\n"
        f"Topic: {safe_desc}\n"
        f"Language: {safe_lang}\n\n"
        f"Current post:\n{safe_content}{poll_note}\n\n"
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

    return response.choices[0].message.content.strip()[:max_chars]


async def generate_newsletter_digest(
    campaign_name: str,
    description: str,
    tone: str,
    language: str,
    post_contents: List[str],
) -> str:
    """Generate a newsletter digest from a campaign's published/pending posts."""
    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=httpx.AsyncClient())

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
    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=httpx.AsyncClient())

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


def _build_export_prompts(
    safe_name: str,
    safe_desc: str,
    safe_lang: str,
    safe_posts: str,
    target: str,
    length: str,
    tone_shift: str,
    base_tone: str,
    include_cta: bool,
    include_emoji: bool,
) -> tuple[str, str]:
    """Build system + user prompts for export generation/refinement."""
    length_map = {
        "short":  {"newsletter": "150–250 words", "linkedin": "100–180 words", "reddit": "100–200 words"},
        "medium": {"newsletter": "350–550 words", "linkedin": "200–350 words", "reddit": "200–350 words"},
        "long":   {"newsletter": "600–900 words", "linkedin": "400–600 words", "reddit": "350–500 words"},
    }
    word_target = length_map.get(length, length_map["medium"]).get(target, "300–500 words")

    tone_instruction = {
        "keep":   f"Use the campaign's original tone: {base_tone}.",
        "formal": "Shift the tone to be formal and professional.",
        "casual": "Shift the tone to be casual and conversational.",
    }.get(tone_shift, f"Tone: {base_tone}.")

    cta_instruction = (
        "End with a brief, concrete call-to-action." if include_cta
        else "Do not include a call-to-action at the end."
    )

    emoji_instruction = (
        "Use relevant emoji to improve scannability and visual flow "
        "(e.g. 🔹 for bullet points, 💡 for key insights, 🚀 for calls-to-action)."
        if include_emoji
        else "Do not use emoji — keep the text clean and plain."
    )

    if target == "newsletter":
        system_prompt = (
            f"You are a newsletter writer. Write a cohesive newsletter digest based on the provided Mastodon campaign posts. "
            f"Start with a suggested subject line prefixed 'Subject: '. "
            f"Then write the newsletter body: an engaging intro, key insights woven into flowing prose. "
            f"{cta_instruction} {emoji_instruction} "
            f"Target length: {word_target}. {tone_instruction} Language: {safe_lang}. "
            f"Output only the subject line and newsletter body — no meta-commentary."
        )
        user_prompt = f"Campaign: {safe_name}\nTopic: {safe_desc}\n\nPosts:\n{safe_posts}"

    elif target == "linkedin":
        system_prompt = (
            f"You are an expert LinkedIn content writer. "
            f"Write a compelling standalone LinkedIn article based on the provided campaign. "
            f"Start with a suggested headline prefixed 'Headline: '. "
            f"Write the body as focused paragraphs — punchy, self-contained, and immediately useful. "
            f"{cta_instruction} {emoji_instruction} "
            f"Target length: {word_target}. {tone_instruction} Language: {safe_lang}. "
            f"Never mention that this was derived from social media posts. "
            f"Output only the headline line and article body — no meta-commentary."
        )
        user_prompt = f"Topic: {safe_name} — {safe_desc}\n\nKey insights:\n{safe_posts}"

    else:  # reddit
        system_prompt = (
            f"You are a skilled Reddit writer. Write a genuine, community-oriented Reddit post "
            f"based on the provided campaign material. "
            f"Start with a suggested post title prefixed 'Title: '. "
            f"Write the body in a direct, authentic Reddit voice — share insights honestly, "
            f"invite discussion, avoid marketing language. "
            f"{cta_instruction} {emoji_instruction} "
            f"Target length: {word_target}. {tone_instruction} Language: {safe_lang}. "
            f"Output only the title line and post body — no meta-commentary."
        )
        user_prompt = f"Topic: {safe_name} — {safe_desc}\n\nKey points:\n{safe_posts}"

    return system_prompt, user_prompt


async def generate_export(
    campaign_name: str,
    description: str,
    tone: str,
    language: str,
    post_contents: List[str],
    target: str = "newsletter",
    length: str = "medium",
    tone_shift: str = "keep",
    include_cta: bool = True,
    include_emoji: bool = False,
) -> str:
    """Unified export generator supporting newsletter, LinkedIn, and Reddit targets."""
    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=httpx.AsyncClient())

    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)
    base_tone = TONE_DESCRIPTIONS.get(tone, tone)
    safe_posts = "\n\n".join(_sanitize(p, 500) for p in post_contents[:40])

    system_prompt, user_prompt = _build_export_prompts(
        safe_name, safe_desc, safe_lang, safe_posts,
        target, length, tone_shift, base_tone, include_cta, include_emoji,
    )

    token_map = {"short": 500, "medium": 900, "long": 1400}
    max_tokens = token_map.get(length, 900)

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.7,
        max_completion_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()


async def refine_export(
    current_content: str,
    instructions: str,
    campaign_name: str,
    description: str,
    tone: str,
    language: str,
    post_contents: List[str],
    target: str = "newsletter",
    length: str = "medium",
    tone_shift: str = "keep",
    include_cta: bool = True,
    include_emoji: bool = False,
) -> str:
    """Refine an existing export based on user instructions, keeping all original context."""
    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=httpx.AsyncClient())

    safe_name = _sanitize(campaign_name, 200)
    safe_desc = _sanitize(description, 2000)
    safe_lang = _sanitize(language, 50)
    safe_content = _sanitize(current_content, 6000)
    safe_instructions = _sanitize(instructions, 500)
    base_tone = TONE_DESCRIPTIONS.get(tone, tone)
    safe_posts = "\n\n".join(_sanitize(p, 500) for p in post_contents[:40])

    _, context_prompt = _build_export_prompts(
        safe_name, safe_desc, safe_lang, safe_posts,
        target, length, tone_shift, base_tone, include_cta, include_emoji,
    )

    target_labels = {"newsletter": "newsletter digest", "linkedin": "LinkedIn article", "reddit": "Reddit post"}
    target_label = target_labels.get(target, "content")

    system_prompt = (
        f"You are an expert editor refining a {target_label}. "
        f"The user has edited the current version and has specific instructions for changes. "
        f"Rewrite the content incorporating those instructions while preserving the overall structure, "
        f"target length, tone, and format (subject/headline/title prefix must be kept if present). "
        f"Output only the refined content — no commentary, no explanation."
    )

    user_prompt = (
        f"Original context:\n{context_prompt}\n\n"
        f"Current content:\n{safe_content}\n\n"
        f"Refinement instructions: {safe_instructions}"
    )

    token_map = {"short": 500, "medium": 900, "long": 1400}
    max_tokens = token_map.get(length, 900)

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.6,
        max_completion_tokens=max_tokens,
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
