"""tootflow ops — round-the-clock publishing and metrics, triggered manually or by the harness."""
import argparse
import asyncio

from app.cli.common import db_session, out, out_json
from app.scheduler import publish_due_posts, update_metrics


def cmd_publish(args: argparse.Namespace) -> int:
    """Publish all due posts, now. Safe to run repeatedly (idempotent per post status)."""
    published = _run_publish()
    if args.json:
        out_json(published)
    else:
        if not published:
            out("Nothing due.")
        for row in published:
            out(f"published post {row['post_id']} (campaign {row['campaign_id']}) → {row['status_id']}")
    return 0


def _run_publish() -> list[dict]:
    """Run one publish cycle and report what changed (by observing status flips)."""
    from datetime import datetime

    from sqlmodel import select

    from app.models import Campaign, Post

    with db_session() as session:
        pending_before = {
            p.id: p for p in session.exec(
                select(Post)
                .join(Campaign)
                .where(
                    Post.status == "pending",
                    Post.scheduled_time <= datetime.utcnow(),
                    Campaign.status.in_(["active", "running"]),
                    Campaign.enabled == True,  # noqa: E712
                )
            ).all()
        }
    if not pending_before:
        return []

    asyncio.run(publish_due_posts())

    results = []
    with db_session() as session:
        for post_id, before in pending_before.items():
            after = session.get(Post, post_id)
            if after is None or after.status == "pending":
                continue  # transient failure — still pending, retried next run
            results.append({
                "post_id": post_id,
                "campaign_id": after.campaign_id,
                "status": after.status,
                "status_id": after.mastodon_status_id,
            })
    return results


def cmd_metrics_refresh(args: argparse.Namespace) -> int:
    """Fetch engagement metrics for recently published posts (same job as the app's daily run)."""
    asyncio.run(update_metrics())
    if args.json:
        out_json({"ok": True})
    else:
        out("Metrics refreshed.")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("ops", help="Publishing + metrics triggers (manual or harness-driven)")
    subp = p.add_subparsers(dest="ops_cmd", required=True)

    pub = subp.add_parser("publish", help="Publish all due posts, now")
    pub.add_argument("--json", action="store_true")
    pub.set_defaults(func=cmd_publish)

    met = subp.add_parser("metrics-refresh", help="Fetch engagement metrics for recent published posts")
    met.add_argument("--json", action="store_true")
    met.set_defaults(func=cmd_metrics_refresh)
