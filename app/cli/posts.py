"""tootflow posts — draft, schedule, publish, inspect."""
import argparse
import json
import sys
from datetime import datetime

from sqlmodel import select

from app.cli.common import (
    db_session,
    fail,
    out,
    out_json,
    resolve_campaign,
    resolve_user,
    to_dict,
)
from app.models import Campaign, Metrics, Post
from app.scheduling import schedule_posts

MAX_LEN = 500


def _read_content(args) -> str:
    if args.content is not None and args.file is not None:
        fail("Use either --content or --file, not both")
    if args.file is not None:
        if args.file == "-":
            data = sys.stdin.read()
        else:
            with open(args.file) as f:
                data = f.read()
        return data.strip()[:MAX_LEN]
    if args.content is not None:
        return args.content.strip()[:MAX_LEN]
    fail("Provide post content via --content, --file, or stdin (-)")


def _parse_at(value: str | None) -> datetime:
    if value is None:
        fail("--at is required for this operation (e.g. --at '2026-11-01T14:00')")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        fail(f"Invalid --at timestamp: {value}")


def _apply_status_transition(campaign: Campaign) -> None:
    if campaign.status == "draft":
        campaign.status = "active"
        campaign.start_date = campaign.start_date or datetime.utcnow()
    elif campaign.status == "completed":
        campaign.status = "running"


def cmd_posts_add(args: argparse.Namespace) -> int:
    content = _read_content(args)
    at = _parse_at(args.at)

    with db_session() as session:
        user = resolve_user(session, args.user_id)
        campaign = resolve_campaign(session, user, args.campaign)

        if args.skip_duplicate:
            existing = session.exec(
                select(Post).where(
                    Post.campaign_id == campaign.id,
                    Post.status == "pending",
                    Post.content == content,
                )
            ).first()
            if existing:
                if args.json:
                    out_json({"skipped": True, "post_id": existing.id,
                              "reason": "identical pending post exists"})
                else:
                    out(f"Skipped — identical pending post {existing.id} exists.")
                return 0

        if args.poll:
            opts = [o.strip() for o in args.poll.split(",") if o.strip()]
            if len(opts) < 2:
                fail("A poll needs at least 2 options (comma-separated)")
            poll_options = json.dumps(opts[:4])
        else:
            poll_options = None

        post = Post(
            campaign_id=campaign.id,
            content=content,
            scheduled_time=at,
            poll_options=poll_options,
            poll_expires_in=args.poll_expires_in,
        )
        session.add(post)
        _apply_status_transition(campaign)
        session.add(campaign)
        session.commit()
        session.refresh(post)
        if args.json:
            out_json(to_dict(post))
        else:
            out(f"Added post {post.id}: [{post.scheduled_time:%Y-%m-%d %H:%M}] {post.content[:60]}…")
    return 0


def cmd_posts_import(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if args.json_file == "-" else open(args.json_file).read()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"Invalid JSON: {e}")
    if not isinstance(items, list) or not items:
        fail("Expected a non-empty JSON array of post objects")

    with db_session() as session:
        user = resolve_user(session, args.user_id)
        campaign = resolve_campaign(session, user, args.campaign)

        unscheduled = [item for item in items if not item.get("at")]

        start = max(campaign.start_date or datetime.utcnow(), datetime.utcnow())
        schedule = schedule_posts([item.get("content", "") for item in unscheduled],
                                  start, campaign.posts_per_week)

        added, skipped = [], []
        seen = set()
        unscheduled_pos = 0
        for item in items:
            content = str(item.get("content", "")).strip()[:MAX_LEN]
            if not content:
                skipped.append({"reason": "empty content", "item": item})
                continue
            if args.skip_duplicate:
                if content in seen or session.exec(
                    select(Post).where(
                        Post.campaign_id == campaign.id,
                        Post.status == "pending",
                        Post.content == content,
                    )
                ).first():
                    skipped.append({"reason": "duplicate", "item": item})
                    continue
            seen.add(content)
            if item.get("at"):
                try:
                    at = datetime.fromisoformat(str(item["at"]))
                except ValueError:
                    skipped.append({"reason": f"invalid at: {item['at']}", "item": item})
                    continue
            else:
                # consume the next slot of the pre-computed schedule, in item order
                at = schedule[unscheduled_pos][1]
                unscheduled_pos += 1
            poll = item.get("poll")
            poll_options = json.dumps([str(o) for o in poll][:4]) if poll and len(poll) >= 2 else None
            post = Post(
                campaign_id=campaign.id,
                content=content,
                scheduled_time=at,
                poll_options=poll_options,
                poll_expires_in=int(item.get("poll_expires_in", 86400)),
            )
            session.add(post)
            _apply_status_transition(campaign)
            added.append(post)

        session.commit()
        for p in added:
            session.refresh(p)
        result = {
            "campaign_id": campaign.id,
            "added": [to_dict(p) for p in added],
            "skipped": skipped,
            "next_post": to_dict(added[0]) if added else None,
        }
        if args.json:
            out_json(result)
        else:
            out(f"Imported {len(added)} post(s) into campaign {campaign.id} "
                f"({len(skipped)} skipped).")
            for p in added:
                out(f"  post {p.id}: [{p.scheduled_time:%Y-%m-%d %H:%M}] {p.content[:60]}…")
    return 0


def cmd_posts_list(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        query = (
            select(Post)
            .join(Campaign)
            .where(Campaign.user_id == user.id)
            .order_by(Post.scheduled_time)
        )
        if args.campaign:
            query = query.where(Post.campaign_id == args.campaign)
        if args.status:
            query = query.where(Post.status == args.status)
        posts = session.exec(query).all()
        if args.json:
            out_json([to_dict(p) for p in posts])
        else:
            if not posts:
                out("No posts match.")
            for p in posts:
                out(f"{p.id:>5}  [{p.status:>9}]  {p.scheduled_time:%Y-%m-%d %H:%M}  "
                    f"(campaign {p.campaign_id})  {p.content[:60]}…")
    return 0


def _get_post(session, user, post_id) -> Post:
    post = session.get(Post, post_id)
    if not post:
        fail(f"No post {post_id}")
    campaign = session.get(Campaign, post.campaign_id)
    if not campaign or campaign.user_id != user.id:
        fail(f"No post {post_id} for this user")
    return post, campaign


def cmd_posts_get(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        post, campaign = _get_post(session, user, args.post_id)
        metrics = session.exec(select(Metrics).where(Metrics.post_id == post.id)).first()
        data = to_dict(post)
        data["campaign_status"] = campaign.status
        data["metrics"] = to_dict(metrics) if metrics else None
        if args.json:
            out_json(data)
        else:
            out(f"post {post.id} [{post.status}] campaign {post.campaign_id} ({campaign.status})")
            out(f"  scheduled: {post.scheduled_time:%Y-%m-%d %H:%M} UTC")
            out(f"  published: {post.published_at:%Y-%m-%d %H:%M} UTC" if post.published_at else "  published: —")
            out(f"  status id: {post.mastodon_status_id or '—'}")
            if post.poll_options:
                out(f"  poll: {post.poll_options} (expires in {post.poll_expires_in}s)")
            out("")
            out(post.content)
            if metrics:
                out("")
                out(f"  metrics: ♥ {metrics.favourites}  🔁 {metrics.reblogs}  "
                    f"↩ {metrics.replies}  voters {metrics.poll_voters}")
    return 0


def cmd_posts_edit(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        post, campaign = _get_post(session, user, args.post_id)
        if post.status != "pending":
            fail(f"Post {post.id} is {post.status} — only pending posts can be edited")
        changed = False
        if args.content is not None or args.file is not None:
            if args.content is not None and args.file is not None:
                fail("Use either --content or --file, not both")
            if args.file is not None:
                data = sys.stdin.read() if args.file == "-" else open(args.file).read()
                post.content = data.strip()[:MAX_LEN]
            else:
                post.content = args.content.strip()[:MAX_LEN]
            changed = True
        if args.at is not None:
            post.scheduled_time = _parse_at(args.at)
            changed = True
        if args.poll is not None:
            opts = [o.strip() for o in args.poll.split(",") if o.strip()]
            post.poll_options = json.dumps(opts[:4]) if len(opts) >= 2 else None
            changed = True
        if not changed:
            fail("Nothing to change — pass --content/--file, --at, or --poll")
        session.add(post)
        session.commit()
        session.refresh(post)
        if args.json:
            out_json(to_dict(post))
        else:
            out(f"Updated post {post.id}: [{post.scheduled_time:%Y-%m-%d %H:%M}] {post.content[:60]}…")
    return 0


def cmd_posts_move(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        post, campaign = _get_post(session, user, args.post_id)
        if post.status != "pending":
            fail(f"Post {post.id} is {post.status} — only pending posts can be moved")
        post.scheduled_time = _parse_at(args.at)
        session.add(post)
        session.commit()
        session.refresh(post)
        if args.json:
            out_json(to_dict(post))
        else:
            out(f"Moved post {post.id} to {post.scheduled_time:%Y-%m-%d %H:%M} UTC")
    return 0


def cmd_posts_remove(args: argparse.Namespace) -> int:
    if not args.yes:
        fail(f"refusing to remove post {args.post_id} without --yes", 2)
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        post, campaign = _get_post(session, user, args.post_id)
        metrics = session.exec(select(Metrics).where(Metrics.post_id == post.id)).first()
        if metrics:
            session.delete(metrics)
        session.delete(post)
        session.commit()
        if args.json:
            out_json({"removed": args.post_id})
        else:
            out(f"Removed post {args.post_id}.")
    return 0


def cmd_posts_send(args: argparse.Namespace) -> int:
    """Publish a single post immediately, ignoring its schedule."""
    import asyncio

    from app.scheduler import _publish_single_post
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        post, campaign = _get_post(session, user, args.post_id)
        if post.status != "pending":
            fail(f"Post {post.id} is {post.status} — only pending posts can be sent")
        if not campaign.enabled:
            fail(f"Campaign {campaign.id} is paused — resume it first (or publish via the app)")

        asyncio.run(_publish_single_post(session, campaign, post))
        session.commit()
        session.refresh(post)
        if post.status == "published":
            if args.json:
                out_json(to_dict(post))
            else:
                out(f"Published post {post.id} → status {post.mastodon_status_id}")
            return 0
        # permanent failure or transient error — status tells which
        if args.json:
            out_json(to_dict(post))
        else:
            out(f"Post {post.id}: {post.status}"
                + (" (will retry on next publish)" if post.status == "pending" else ""))
        return 0 if post.status == "pending" else 1


def cmd_posts_metrics(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        if args.post_id:
            post, campaign = _get_post(session, user, args.post_id)
            metrics = session.exec(select(Metrics).where(Metrics.post_id == post.id)).first()
            payload = to_dict(metrics) if metrics else {"post_id": post.id, "note": "no metrics collected yet — run: tootflow metrics-refresh"}
            if args.json:
                out_json(payload)
            else:
                out(f"post {post.id}: ♥ {metrics.favourites}  🔁 {metrics.reblogs}  ↩ {metrics.replies}"
                    f"  voters {metrics.poll_voters}  (as of {metrics.last_updated:%Y-%m-%d %H:%M})"
                    if metrics else f"post {post.id}: no metrics yet — run: tootflow metrics-refresh")
            return 0

        campaign = resolve_campaign(session, user, args.campaign)
        posts = session.exec(
            select(Post).where(Post.campaign_id == campaign.id).order_by(Post.scheduled_time)
        ).all()
        rows = []
        totals = {"favourites": 0, "reblogs": 0, "replies": 0, "poll_voters": 0}
        for p in posts:
            m = session.exec(select(Metrics).where(Metrics.post_id == p.id)).first()
            row = {"post_id": p.id, "status": p.status,
                   "scheduled_time": p.scheduled_time.isoformat(),
                   "content": p.content[:80],
                   "metrics": to_dict(m) if m else None}
            rows.append(row)
            if m:
                for k in totals:
                    totals[k] += getattr(m, k)
        result = {"campaign_id": campaign.id, "totals": totals, "posts": rows}
        if args.json:
            out_json(result)
        else:
            for row in rows:
                m = row["metrics"]
                stats = (f"♥ {m['favourites']}  🔁 {m['reblogs']}  ↩ {m['replies']}"
                         if m else "no metrics yet")
                out(f"{row['post_id']:>5}  [{row['status']:>9}]  {stats}")
            out(f"\ntotals: ♥ {totals['favourites']}  🔁 {totals['reblogs']}  "
                f"↩ {totals['replies']}  voters {totals['poll_voters']}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("posts", help="Draft, schedule, publish, and inspect posts")
    subp = p.add_subparsers(dest="posts_cmd", required=True)

    def add_common(sp, *, with_content=False, with_post_id=False, with_campaign=False):
        if with_post_id:
            sp.add_argument("post_id", type=int)
        if with_campaign:
            sp.add_argument("--campaign", type=int, required=True, help="Campaign id")
        sp.add_argument("--user-id", type=int, default=None)
        sp.add_argument("--json", action="store_true")

    add = subp.add_parser("add", help="Add a post")
    add.add_argument("--campaign", type=int, required=True)
    add.add_argument("--content", default=None, help="Post text ('-' for stdin)")
    add.add_argument("--file", default=None, help="Read content from file ('-' for stdin)")
    add.add_argument("--at", required=True, help="Scheduled time (ISO, UTC)")
    add.add_argument("--poll", default=None, help="Comma-separated poll options (2–4)")
    add.add_argument("--poll-expires-in", type=int, default=86400)
    add.add_argument("--skip-duplicate", action="store_true",
                     help="Skip if an identical pending post exists")
    add.add_argument("--user-id", type=int, default=None)
    add.add_argument("--json", action="store_true")
    add.set_defaults(func=cmd_posts_add)

    imp = subp.add_parser("import", help="Batch-import posts from JSON (array of {content, at?, poll?})")
    imp.add_argument("--campaign", type=int, required=True)
    imp.add_argument("--json-file", required=True, help="Path to JSON file ('-' for stdin)")
    imp.add_argument("--skip-duplicate", action="store_true")
    imp.add_argument("--user-id", type=int, default=None)
    imp.add_argument("--json", action="store_true")
    imp.set_defaults(func=cmd_posts_import)

    lst = subp.add_parser("list", help="List posts")
    lst.add_argument("--campaign", type=int, default=None)
    lst.add_argument("--status", default=None)
    lst.add_argument("--user-id", type=int, default=None)
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_posts_list)

    get = subp.add_parser("get", help="Show one post")
    get.add_argument("post_id", type=int)
    get.add_argument("--user-id", type=int, default=None)
    get.add_argument("--json", action="store_true")
    get.set_defaults(func=cmd_posts_get)

    edit = subp.add_parser("edit", help="Edit a pending post (content and/or schedule)")
    edit.add_argument("post_id", type=int)
    edit.add_argument("--content", default=None)
    edit.add_argument("--file", default=None, help="Read content from file ('-' for stdin)")
    edit.add_argument("--at", default=None)
    edit.add_argument("--poll", default=None, help="Comma-separated options; single value removes poll")
    edit.add_argument("--user-id", type=int, default=None)
    edit.add_argument("--json", action="store_true")
    edit.set_defaults(func=cmd_posts_edit)

    move = subp.add_parser("move", help="Reschedule a pending post")
    move.add_argument("post_id", type=int)
    move.add_argument("--at", required=True)
    move.add_argument("--user-id", type=int, default=None)
    move.add_argument("--json", action="store_true")
    move.set_defaults(func=cmd_posts_move)

    rm = subp.add_parser("remove", help="Remove a post")
    rm.add_argument("post_id", type=int)
    rm.add_argument("--yes", action="store_true")
    rm.add_argument("--user-id", type=int, default=None)
    rm.add_argument("--json", action="store_true")
    rm.set_defaults(func=cmd_posts_remove)

    send = subp.add_parser("send", help="Publish a pending post immediately (ignores schedule)")
    send.add_argument("post_id", type=int)
    send.add_argument("--user-id", type=int, default=None)
    send.add_argument("--json", action="store_true")
    send.set_defaults(func=cmd_posts_send)

    met = subp.add_parser("metrics", help="Show stored engagement metrics")
    met.add_argument("--post-id", type=int, default=None)
    met.add_argument("--campaign", type=int, default=None)
    met.add_argument("--user-id", type=int, default=None)
    met.add_argument("--json", action="store_true")
    met.set_defaults(func=cmd_posts_metrics)
