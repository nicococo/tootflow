"""tootflow campaigns — campaign management."""
import argparse
from datetime import datetime, timedelta

from sqlmodel import select

from app.cli.common import (
    db_session,
    fail,
    out,
    out_json,
    resolve_account,
    resolve_user,
    to_dict,
)
from app.models import Campaign, Metrics, Post
from app.scheduling import schedule_posts

TEMPLATES = [
    "product_launch", "open_source_release", "event_promotion", "knowledge_sharing",
    "developer_tips", "research_insights", "personal_brand", "community_building",
    "project_showcase", "newsletter_growth",
]
TONES = [
    "professional", "casual", "technical", "inspiring", "humorous", "riddles",
    "storytelling", "hot take", "minimalist", "socratic", "behind the scenes", "rant",
]
LANGUAGES = ["English", "German", "French"]


def cmd_campaigns_create(args: argparse.Namespace) -> int:
    if args.template not in TEMPLATES:
        fail(f"Invalid --template. Options: {', '.join(TEMPLATES)}")
    if args.tone not in TONES:
        fail(f"Invalid --tone. Options: {', '.join(TONES)}")
    if args.language not in LANGUAGES:
        fail(f"Invalid --language. Options: {', '.join(LANGUAGES)}")

    with db_session() as session:
        user = resolve_user(session, args.user_id)
        account = resolve_account(session, user, args.account)

        start = args.start or datetime.utcnow()
        if isinstance(start, str):
            try:
                start = datetime.fromisoformat(start)
            except ValueError:
                fail(f"Invalid --start timestamp: {args.start}")

        campaign = Campaign(
            user_id=user.id,
            mastodon_account_id=account.id,
            name=args.name.strip()[:200],
            description=args.description.strip() or "{}",
            template_type=args.template,
            tone=args.tone,
            language=args.language,
            duration_days=max(1, min(365, args.duration_days)),
            posts_per_week=max(1, min(14, args.posts_per_week)),
            status="active",
            start_date=start,
            end_date=start + timedelta(days=max(1, min(365, args.duration_days))),
        )
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        if args.json:
            out_json(to_dict(campaign))
        else:
            out(f"Created campaign {campaign.id}: {campaign.name} "
                f"(active, starts {start.strftime('%Y-%m-%d %H:%M')}, posting as @{account.username})")
    return 0


def cmd_campaigns_list(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        query = select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.created_at.desc())
        if args.status:
            query = query.where(Campaign.status == args.status)
        campaigns = session.exec(query).all()
        rows = []
        for c in campaigns:
            posts = session.exec(select(Post).where(Post.campaign_id == c.id)).all()
            row = to_dict(c)
            row["posts_total"] = len(posts)
            row["posts_pending"] = sum(1 for p in posts if p.status == "pending")
            rows.append(row)
        if args.json:
            out_json(rows)
        else:
            if not rows:
                out("No campaigns. Create one: tootflow campaigns create --name …")
            for r in rows:
                paused = " [paused]" if not r["enabled"] else ""
                out(f"{r['id']:>4}  {r['name']}  [{r['status']}]{paused}  "
                    f"{r['posts_total']} posts ({r['posts_pending']} pending)")
    return 0


def cmd_campaigns_get(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        campaign = session.get(Campaign, args.campaign_id)
        if not campaign or campaign.user_id != user.id:
            fail(f"No campaign {args.campaign_id} for this user")
        posts = session.exec(
            select(Post).where(Post.campaign_id == campaign.id).order_by(Post.scheduled_time)
        ).all()
        data = to_dict(campaign)
        data["posts"] = [to_dict(p) for p in posts]
        if args.json:
            out_json(data)
        else:
            out(f"{campaign.id}: {campaign.name} [{campaign.status}]"
                f"{' [paused]' if not campaign.enabled else ''}")
            out(f"  {campaign.duration_days}d, {campaign.posts_per_week}/week, "
                f"start {campaign.start_date or '—'}, end {campaign.end_date or '—'}")
            for p in posts:
                out(f"  post {p.id}: [{p.status}] {p.scheduled_time:%Y-%m-%d %H:%M} — {p.content[:60]}…")
    return 0


def cmd_campaigns_archive(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        campaign = session.get(Campaign, args.campaign_id)
        if not campaign or campaign.user_id != user.id:
            fail(f"No campaign {args.campaign_id} for this user")
        if campaign.status == "archived":
            fail(f"Campaign {campaign.id} is already archived")
        pending = session.exec(
            select(Post).where(Post.campaign_id == campaign.id, Post.status == "pending")
        ).all()
        for p in pending:
            p.status = "cancelled"
            session.add(p)
        campaign.status = "archived"
        session.add(campaign)
        session.commit()
        if args.json:
            out_json({"id": campaign.id, "status": "archived", "cancelled_posts": len(pending)})
        else:
            out(f"Archived campaign {campaign.id}; {len(pending)} pending post(s) cancelled.")
    return 0


def cmd_campaigns_remove(args: argparse.Namespace) -> int:
    if not args.yes:
        fail(f"refusing to remove campaign {args.campaign_id} without --yes", 2)
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        campaign = session.get(Campaign, args.campaign_id)
        if not campaign or campaign.user_id != user.id:
            fail(f"No campaign {args.campaign_id} for this user")
        posts = session.exec(select(Post).where(Post.campaign_id == campaign.id)).all()
        for p in posts:
            metrics = session.exec(select(Metrics).where(Metrics.post_id == p.id)).first()
            if metrics:
                session.delete(metrics)
            session.delete(p)
        session.delete(campaign)
        session.commit()
        if args.json:
            out_json({"removed": campaign.id, "posts_deleted": len(posts)})
        else:
            out(f"Removed campaign {campaign.id} and {len(posts)} post(s).")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("campaigns", help="Manage campaigns")
    subp = p.add_subparsers(dest="campaigns_cmd", required=True)

    create = subp.add_parser("create", help="Create a campaign (starts active)")
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="{}", help="Campaign brief (text or JSON)")
    create.add_argument("--template", default="knowledge_sharing", choices=TEMPLATES)
    create.add_argument("--tone", default="professional", choices=TONES)
    create.add_argument("--language", default="English", choices=LANGUAGES)
    create.add_argument("--duration-days", type=int, default=30)
    create.add_argument("--posts-per-week", type=int, default=3)
    create.add_argument("--start", default=None, help="Start timestamp (ISO); default: now")
    create.add_argument("--account", default=None, help="Posting account: id or @user@instance")
    create.add_argument("--user-id", type=int, default=None)
    create.add_argument("--json", action="store_true")
    create.set_defaults(func=cmd_campaigns_create)

    lst = subp.add_parser("list", help="List campaigns")
    lst.add_argument("--status", default=None, help="Filter by status")
    lst.add_argument("--user-id", type=int, default=None)
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_campaigns_list)

    get = subp.add_parser("get", help="Show one campaign with its posts")
    get.add_argument("campaign_id", type=int)
    get.add_argument("--user-id", type=int, default=None)
    get.add_argument("--json", action="store_true")
    get.set_defaults(func=cmd_campaigns_get)

    arch = subp.add_parser("archive", help="Archive a campaign; pending posts are cancelled")
    arch.add_argument("campaign_id", type=int)
    arch.add_argument("--user-id", type=int, default=None)
    arch.add_argument("--json", action="store_true")
    arch.set_defaults(func=cmd_campaigns_archive)

    rm = subp.add_parser("remove", help="Remove a campaign and all its posts")
    rm.add_argument("campaign_id", type=int)
    rm.add_argument("--yes", action="store_true")
    rm.add_argument("--user-id", type=int, default=None)
    rm.add_argument("--json", action="store_true")
    rm.set_defaults(func=cmd_campaigns_remove)
