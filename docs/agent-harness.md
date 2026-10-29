# Using TootFlow with agent harnesses

TootFlow's CLI is built for a division of labor:

- **The agent** (OpenCode, Claude Code, Codex, …) does the creative work — researching,
  drafting, refining post copy in your voice.
- **TootFlow** does the operational work — account tokens, scheduling, publishing,
  metrics — deterministically, with retries and an audit trail in the database.

## The loop

A typical agent session looks like this:

```text
1. VERIFY    uv run tootflow accounts verify --json
             → assert the account is healthy before drafting anything

2. PLAN      agent proposes a campaign: name, template, tone, cadence
             uv run tootflow campaigns create --name "v2.0 launch" \
                 --template open_source_release --tone technical \
                 --posts-per-week 3 --json

3. DRAFT     agent drafts N posts (each ≤ 500 chars, 1–3 hashtags,
             natural Mastodon voice — this is YOUR job, agent)

4. IMPORT    uv run tootflow posts import --campaign 1 --json-file drafts.json \
                 --skip-duplicate --json
             → returns post IDs + assigned schedule; report it to the user

5. REVIEW    user edits in the web UI, or via:
             uv run tootflow posts edit 12 --content "…"
             uv run tootflow posts move 14 --at "2026-11-05T10:00"

6. PUBLISH   on a loop/trigger (or manually):
             uv run tootflow ops publish --json
             → publishes everything due; idempotent, safe to repeat

7. LEARN     uv run tootflow ops metrics-refresh
             uv run tootflow posts metrics --campaign 1 --json
             → agent reads what worked and drafts the next campaign smarter
```

## Design rules for harness reliability

These are deliberate contracts of the CLI — rely on them:

- **Self-describing.** `uv run tootflow schema` emits a JSON manifest of every command
  (invocations, typed parameters, choices, required flags, conventions). Have the agent
  read it first — it always matches the installed version.
- **`--json` everywhere.** Every read command emits a stable schema. Never parse the
  human-readable output.
- **Exit codes**: `0` success, `1` error (message on stderr), `2` usage error. Branch on
  them; don't scrape stderr.
- **No prompts.** Destructive operations need an explicit `--yes` flag — pass it
  deliberately, not reflexively.
- **Stdin for content.** Long posts go via `--file -` (heredoc/pipe), never through
  shell argument escaping.
- **`--skip-duplicate`** on add/import prevents double-posting when an agent retries.
- **Content limits**: 500 characters per post (Mastodon). The CLI truncates silently —
  check lengths before importing.
- **Polls** are comma-separated (`--poll "Yes,No,Maybe"`, 2–4 options).
- **All times are UTC.**

## Example: OpenCode command

Drop this in `.opencode/command/launch.md` of a project to get a `/launch <topic>`
command that drives the whole loop:

```markdown
---
description: Draft and schedule a TootFlow campaign for a topic
---
You are scheduling a Mastodon campaign about: $ARGUMENTS

1. Run `uv run tootflow accounts verify --json` and stop on failure.
2. Ask me for tone/cadence only if not obvious from context.
3. Draft 6 posts (≤ 500 chars each, 1–3 hashtags, varied formats) as a JSON array
   [{"content": "…"}] and pipe it to:
   `uv run tootflow posts import --campaign <id> --json-file - --skip-duplicate --json`
   (create the campaign first with `campaigns create` if needed)
4. Show me the resulting schedule from the import output and stop for my review.
```

## Where the state lives

- SQLite database (default `./tootflow.db`, or `DATABASE_URL`) — campaigns, posts,
  metrics, and encrypted tokens
- The web app reads/writes the same database — mix and match freely
