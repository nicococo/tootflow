"""tootflow accounts — OAuth account management."""
import argparse
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from sqlmodel import select

from app.cli.common import (
    EXIT_ERROR,
    db_session,
    fail,
    out,
    out_json,
    resolve_user,
    store_token,
    to_dict,
    _handle,
)
from app.models import Campaign, MastodonAccount
from app.services import mastodon_service
from app.urlcheck import InvalidInstanceURL, validate_instance_url


def _wait_for_code(state: str, port: int, timeout_s: int = 300) -> str:
    """Start a one-shot localhost listener and wait for the OAuth callback."""
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            if q.get("state") != [state] or "code" not in q:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Bad OAuth callback (state mismatch).")
                return
            result["code"] = q["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>TootFlow: account connected.</h1>"
                             b"<p>You can close this window.</p>")

        def log_message(self, *args):  # silence request logging
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    timer = threading.Timer(timeout_s, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        timer.cancel()
        server.server_close()
    if "code" not in result:
        fail(f"Timed out waiting for OAuth callback after {timeout_s}s", EXIT_ERROR)
    return result["code"]


def cmd_accounts_add(args: argparse.Namespace) -> int:
    try:
        instance_url = validate_instance_url(args.instance)
    except InvalidInstanceURL as e:
        fail(str(e))

    port = args.port or 0  # 0 = OS-assigned free port
    probe = HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    port = probe.server_address[1]
    probe.server_close()

    redirect_uri = f"http://127.0.0.1:{port}/callback"
    try:
        app_data = mastodon_service.register_app(instance_url, redirect_uri)
    except Exception as e:
        fail(f"Could not register OAuth app with {instance_url}: {e}")

    state = secrets.token_urlsafe(16)
    auth_url = mastodon_service.get_oauth_url(instance_url, app_data["client_id"], redirect_uri, state)

    out("Authorize TootFlow at:")
    out("")
    out(f"  {auth_url}")
    out("")
    if args.no_browser:
        out("Open the URL above in a browser on this machine, authorize, and the CLI will continue.")
    else:
        webbrowser.open(auth_url)

    code = _wait_for_code(state, port)

    try:
        token = mastodon_service.exchange_code_for_token(
            instance_url, app_data["client_id"], app_data["client_secret"], code, redirect_uri
        )
        info = mastodon_service.get_account_info(instance_url, token)
    except Exception as e:
        fail(f"OAuth token exchange failed: {e}")

    username = info.get("username", "")
    with db_session() as session:
        user = resolve_user(session)
        account = store_token(session, user, instance_url, username, token)
        if args.json:
            out_json(to_dict(account))
        else:
            out(f"Connected: {_handle(account)}")


def cmd_accounts_list(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session)
        accounts = session.exec(
            select(MastodonAccount).where(MastodonAccount.user_id == user.id).order_by(MastodonAccount.id)
        ).all()
        rows = [{"id": a.id, "handle": _handle(a), "instance": a.instance_url, "username": a.username}
                for a in accounts]
        if args.json:
            out_json(rows)
        else:
            if not rows:
                out("No accounts connected. Add one: tootflow accounts add --instance https://mastodon.social")
            for r in rows:
                out(f"{r['id']:>4}  @{r['handle']}")
    return 0


def cmd_accounts_verify(args: argparse.Namespace) -> int:
    from app.crypto import decrypt_token
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        accounts = session.exec(
            select(MastodonAccount).where(MastodonAccount.user_id == user.id).order_by(MastodonAccount.id)
        ).all()
        if args.account:
            accounts = [a for a in accounts
                        if (args.account.isdigit() and a.id == int(args.account))
                        or args.account.lstrip("@") == _handle(a).lstrip("@")]
            if not accounts:
                fail(f"No such account: {args.account}")
        results = []
        for a in accounts:
            entry = {"id": a.id, "handle": _handle(a)}
            try:
                info = mastodon_service.get_account_info(a.instance_url, decrypt_token(a.access_token))
                entry.update({"ok": True, "username": info.get("username"),
                              "followers": info.get("followers_count")})
            except Exception as e:
                entry.update({"ok": False, "error": str(e)[:200]})
            results.append(entry)
        if args.json:
            out_json(results)
        else:
            for r in results:
                mark = "✓" if r.get("ok") else "✕"
                detail = f"{r.get('username')} — {r.get('followers')} followers" if r.get("ok") else r.get("error")
                out(f"{mark} {r['handle']}  {detail}")
        if not all(r.get("ok") for r in results):
            return 1
    return 0


def cmd_accounts_remove(args: argparse.Namespace) -> int:
    with db_session() as session:
        user = resolve_user(session, args.user_id)
        accounts = session.exec(
            select(MastodonAccount).where(MastodonAccount.user_id == user.id).order_by(MastodonAccount.id)
        ).all()
        target = None
        for a in accounts:
            if (args.account.isdigit() and a.id == int(args.account)) or args.account.lstrip("@") == _handle(a).lstrip("@"):
                target = a
                break
        if not target:
            fail(f"No such account: {args.account}")
        # detach campaigns pointing at it, then delete
        campaigns = session.exec(
            select(Campaign).where(Campaign.mastodon_account_id == target.id)
        ).all()
        for c in campaigns:
            c.mastodon_account_id = None
            session.add(c)
        session.delete(target)
        session.commit()
        if args.json:
            out_json({"removed": _handle(target)})
        else:
            out(f"Removed: {_handle(target)} (campaigns detached)")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("accounts", help="Manage connected Mastodon accounts")
    subp = p.add_subparsers(dest="accounts_cmd", required=True)

    add = subp.add_parser("add", help="Connect a Mastodon account via OAuth (opens browser)")
    add.add_argument("--instance", required=True, help="e.g. https://mastodon.social")
    add.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser")
    add.add_argument("--port", type=int, default=None, help="Fixed port for the localhost callback listener")
    add.set_defaults(func=cmd_accounts_add)

    lst = subp.add_parser("list", help="List connected accounts")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_accounts_list)

    verify = subp.add_parser("verify", help="Verify credentials against the instance(s)")
    verify.add_argument("account", nargs="?", default=None, help="id or @user@instance (default: all)")
    verify.add_argument("--user-id", type=int, default=None)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_accounts_verify)

    rm = subp.add_parser("remove", help="Disconnect an account")
    rm.add_argument("account", help="id or @user@instance")
    rm.add_argument("--yes", action="store_true")
    rm.add_argument("--user-id", type=int, default=None)
    rm.add_argument("--json", action="store_true")
    rm.set_defaults(func=cmd_accounts_remove)
