# TootFlow CLI

A single-user command line interface for TootFlow — designed to be driven by
[agent harnesses](agent-harness.md) (OpenCode, Claude Code, Codex, …) or typed by hand.

The CLI talks directly to TootFlow's database — no web server required. Content is
expected to come **from the calling agent**; the CLI handles accounts, scheduling,
publishing, and metrics.

```bash
uv run tootflow --help
```

## Setup

```bash
cp .env.example .env          # SECRET_KEY is required
uv run tootflow accounts add --instance https://mastodon.social
```

### Agent self-discovery

```bash
uv run tootflow schema        # JSON tool manifest of every command + parameters
```

`tootflow schema` prints the entire CLI as a machine-readable agent-tool dict —
command names, invocations, typed parameters, choices, defaults, required flags,
and the shared conventions (exit codes, stdin, content limits, UTC times).
Generated from the argparse tree at runtime, so it always matches the installed
version. Point your harness at it instead of this page.

The `accounts add` flow opens a browser, authorizes via Mastodon OAuth, and stores the
token encrypted. On a headless box use `--no-browser` and open the printed URL on any
machine **with a browser on the same machine** (the OAuth callback targets `127.0.0.1`).

Conventions:

- Add `--json` to any command for machine-readable output
- Exit codes: `0` ok · `1` error · `2` usage error (e.g. missing `--yes`)
- Destructive commands require `--yes`
- Multiple connected accounts? Scope with `--account @user@instance` (or account id)

## Accounts

```bash
tootflow accounts add --instance https://mastodon.social [--no-browser]
tootflow accounts list [--json]
tootflow accounts verify [ID|@user@instance] [--json]   # pre-flight: do the tokens work?
tootflow accounts remove ID --yes
```

## Campaigns

```bash
tootflow campaigns create --name "My launch" \
    [--template knowledge_sharing] [--tone professional] [--language English] \
    [--duration-days 30] [--posts-per-week 3] [--start "2026-11-01T09:00"] \
    [--account @user@instance] [--json]
tootflow campaigns list [--status active] [--json]
tootflow campaigns get ID [--json]
tootflow campaigns archive ID [--json]    # cancels pending posts
tootflow campaigns remove ID --yes [--json]
```

New campaigns start **active** with `start_date` anchored (now or `--start`), so the
scheduler picks them up immediately. Templates mirror the web app's ten campaign types.

## Posts

Content comes from you (the agent): pass `--content`, `--file post.md`, or `-` for stdin.
All times are UTC.

```bash
# explicit schedule
tootflow posts add --campaign 1 --content "Hello fediverse!" --at "2026-11-01T14:00"

# with a poll (2–4 options, Mastodon-style)
tootflow posts add --campaign 1 --content "Pick one" --at "2026-11-01T14:00" --poll "Yes,No"

# batch-import agent drafts; entries without "at" are auto-scheduled
tootflow posts import --campaign 1 --json-file drafts.json [--skip-duplicate]
# drafts.json: [{"content": "…", "at": "2026-11-02T10:00", "poll": ["a","b"]}, …]

tootflow posts list [--campaign 1] [--status pending] [--json]
tootflow posts get ID [--json]
tootflow posts edit ID [--content …] [--file -] [--at …] [--poll …] [--json]
tootflow posts move ID --at "2026-11-03T09:00" [--json]
tootflow posts remove ID --yes [--json]

# publish a single pending post immediately, ignoring its schedule
tootflow posts send ID [--json]
```

`import` prints each stored post with its assigned time — report the schedule from there.
`--skip-duplicate` guards against agent retries double-posting identical content.

## Publishing around the clock

Publishing is a one-shot command — run it manually, from an in-harness loop, or from any
external trigger:

```bash
tootflow ops publish [--json]           # publishes all due posts, now
tootflow ops metrics-refresh [--json]   # fetch engagement for recent published posts
```

`ops publish` is idempotent per post (pending → published/failed only), so running it
repeatedly or from multiple triggers is safe. If you also run the TootFlow web app on
the same database, its scheduler publishes the same posts — the CLI just gives you a
manual trigger.

## Metrics

```bash
tootflow posts metrics --post-id 5 [--json]
tootflow posts metrics --campaign 1 [--json]    # per-post + totals
tootflow ops metrics-refresh                    # collect fresh numbers first
```
