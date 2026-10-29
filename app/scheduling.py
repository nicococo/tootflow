"""Post scheduling helpers shared by the web app and the CLI."""
import random
from datetime import datetime, timedelta


def schedule_posts(posts: list[str], start_date: datetime, posts_per_week: int) -> list[tuple]:
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
