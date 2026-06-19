# minqlx-plugin-review

An AI skill for reviewing, writing, and debugging [minqlx](https://github.com/MinoMino/minqlx) plugins for Quake Live dedicated servers.

minqlx plugins run inside the Quake Live server process. If you run a blocking HTTP call in an event handler, write to game-state from a background thread, or let an unthrottled command flood the network, this surfaces as visible gameplay spikes and lag.


 — a a — . This skill teaches the agent the frame/thread boundaries and library-compatibility pitfalls that cause them.

Works with any agent that supports the [`SKILL.md`](minqlx-plugin-review/SKILL.md) format. Install instructions for [Claude Code](#installation-claude-code) and [Codex](#installation-codex) below.

## What it covers

- **Execution contexts** — `@minqlx.thread` vs `@minqlx.next_frame` vs `@minqlx.delay` vs plain handlers, and the one rule: game-state mutations from a thread must be queued into a game frame.
- **Frame injection safety** — keeping `@minqlx.next_frame` callbacks minimal; spotting cascading re-fetch anti-patterns.
- **Rate limiting** — cooldowns on player-triggered commands that spawn HTTP threads (`!elo`, `!balance`, …).
- **HTTP threading** — never block the 40 fps game loop on a network call.
- **Batching** — accumulate per-event data and flush on a timer instead of one request per kill.
- **Library / environment compatibility** — `redis-py` version drift on minqlx hosts and the try/except compat-shim pattern.
- **Quick audit steps** — grep-based checks for the common mistakes.

## Installation (Claude Code)

Claude Code discovers skills from `~/.claude/skills/` (personal) or `<project>/.claude/skills/` (project-scoped). Each skill is a directory containing a `SKILL.md`.

### Personal (available in every project)

```bash
git clone https://github.com/dngrtech/minqlx-plugin-review.git /tmp/minqlx-plugin-review
mkdir -p ~/.claude/skills
cp -r /tmp/minqlx-plugin-review/minqlx-plugin-review ~/.claude/skills/
```

### Project-scoped (checked into a repo, shared with collaborators)

```bash
git clone https://github.com/dngrtech/minqlx-plugin-review.git /tmp/minqlx-plugin-review
mkdir -p .claude/skills
cp -r /tmp/minqlx-plugin-review/minqlx-plugin-review .claude/skills/
```

Either way the final layout must be:

```
.claude/skills/minqlx-plugin-review/SKILL.md
```

Restart Claude Code (or start a new session) and the skill is picked up automatically. It activates when you ask the agent to review, write, or debug a minqlx plugin — or invoke it explicitly with `/minqlx-plugin-review`.

## Installation (Codex)

Codex discovers personal skills from `~/.codex/skills/`. Each skill is a directory containing a `SKILL.md`.

```bash
git clone https://github.com/dngrtech/minqlx-plugin-review.git /tmp/minqlx-plugin-review
mkdir -p ~/.codex/skills
cp -r /tmp/minqlx-plugin-review/minqlx-plugin-review ~/.codex/skills/
```

The final layout must be:

```
~/.codex/skills/minqlx-plugin-review/SKILL.md
```

> **Note:** OpenAI's docs also reference `~/.agents/skills/` (personal) and `.agents/skills/` (project root / cwd / parent) for skill discovery. Use whichever your Codex version reads — if `~/.codex/skills/` doesn't pick it up, install into `~/.agents/skills/` instead (same `<skill-name>/SKILL.md` layout).

Start a new Codex session. The skill activates automatically when your request matches its description, or invoke it explicitly with `/skills` or `$minqlx-plugin-review`.

## Verifying it loaded

In a session, ask:

> Use the minqlx-plugin-review skill to check this plugin for frame-safety issues.

The agent should announce it's using the skill and apply the checklist.

## License

MIT — see [LICENSE](LICENSE).
