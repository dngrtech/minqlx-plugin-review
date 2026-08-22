# minqlx-plugin-review

An AI skill for reviewing, writing, and debugging [minqlx](https://github.com/MinoMino/minqlx) and [minqlxtended](https://github.com/tjone270/minqlxtended) plugins for Quake Live dedicated servers.

Plugins on both runtimes run inside the Quake Live server process. If you run a blocking HTTP call in an event handler, write to game-state from a background thread, or let an unthrottled command flood the network, this surfaces as visible gameplay spikes and lag.

This skill teaches the agent the frame/thread boundaries, production performance checks, reload-safety rules, and library-compatibility pitfalls that cause them.

It covers both runtimes. minqlx and minqlxtended are hard forks and plugins are not portable between them, so the skill first establishes which one it is looking at, then applies the API deltas — changed and new event signatures, enum constants, property access — on top of the shared checklist.

The minqlxtended API details are verified against the engine source at **v1.0.0**, not against upstream's release notes, which undercount both the changed event signatures and the removed engine functions. Re-check them against `python/minqlxtended/_events.py` if you are on a later version.

Works with any agent that supports the [`SKILL.md`](minqlx-plugin-review/SKILL.md) format. Install instructions for [Claude Code](#installation-claude-code), [Codex](#installation-codex), [Gemini CLI](#installation-gemini-cli), [Claude Desktop](#installation-claude-desktop), and [Gemini Web Gems](#installation-gemini-web-gems) below.

## Table of contents

- [What it covers](#what-it-covers)
- [Installation (Claude Code)](#installation-claude-code)
  - [Personal (available in every project)](#personal-available-in-every-project)
  - [Project-scoped (checked into a repo, shared with collaborators)](#project-scoped-checked-into-a-repo-shared-with-collaborators)
- [Installation (Codex)](#installation-codex)
- [Installation (Gemini CLI)](#installation-gemini-cli)
- [Installation (Claude Desktop)](#installation-claude-desktop)
- [Installation (Gemini Web Gems)](#installation-gemini-web-gems)
- [Verifying it loaded](#verifying-it-loaded)
- [License](#license)

## What it covers

- **Execution contexts** — `@minqlx.thread` vs `@minqlx.next_frame` vs `@minqlx.delay` vs plain handlers, and the one rule: game-state mutations from a thread must be queued into a game frame.
- **Frame injection safety** — keeping `@minqlx.next_frame` callbacks minimal; spotting cascading re-fetch anti-patterns.
- **Rate limiting** — cooldowns on player-triggered commands that spawn HTTP threads (`!elo`, `!balance`, …).
- **HTTP safety** — never block the 40 fps game loop on a network call; require timeouts, bounded error handling, cache TTLs, and single-flight request deduplication.
- **Hot-hook performance** — keep `frame`, `kill`, `death`, `chat`, `client_command`, `team_switch`, and ZMQ `stats` handlers O(1), append-only, or offloaded. `frame` gets its own treatment: it fires every tick unconditionally and must self-throttle on its first lines, off `sv_fps` rather than a hardcoded 40.
- **Batching** — accumulate per-event data and flush on a timer or round/game boundary instead of doing one request or DB write per kill.
- **Redis / storage performance** — avoid broad `KEYS`, large unpaginated reads, and per-player DB round trips in hot paths; prefer `scan_iter`, indexes, pipelines, and pagination.
- **Reload-safe workers** — stop long-running threads, sockets, polling loops, and recurring `@minqlx.delay` callbacks on unload.
- **Player/game lifecycle safety** — re-resolve `Player` objects with `client_id + steam_id`, guard `self.game`, and tolerate disconnects/map changes in delayed callbacks.
- **Output flood control** — cap, paginate, or throttle commands that emit many `tell`, `reply`, or `msg` lines.
- **Hook semantics and unsafe parsing** — use `RET_STOP*` deliberately; flag import-time network, self-modifying downloads, `eval`/`exec`, and bare `except`.
- **Console-command injection** — the engine console treats `;` as a separator, so caller-controlled input formatted into `console_command()` or `set_cvar()` runs arbitrary commands. Requires anchored allow-lists, and treats a permission level as a mitigation rather than a fix.
- **minqlxtended differences** — signatures validated at registration (one stale handler blocks the whole plugin from loading), the nine changed event signatures (upstream documents six; two more that only look changed are called out as false alarms) and six new events, enum constants (`Return.STOP_ALL`, `Priority.LOWEST`, `Weapon.RAILGUN`), setters replaced by property access, `team_scores` in place of `red_score`/`blue_score`, and a porting checklist.
- **Library / environment compatibility** — `redis-py` version drift on minqlx hosts and the try/except compat-shim pattern; on minqlxtended, where the version floors actually live and the silent `hiredis` fallback that costs performance without raising anything.
- **Quick audit steps** — grep-based checks for common production footguns, not just obvious frame/thread mistakes.

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

## Installation (Gemini CLI)

Gemini CLI does not use the `SKILL.md` directory format directly. Use the skill text as project context with `GEMINI.md`.

### Project-scoped

From the repo where you review minqlx plugins, create `GEMINI.md` from the skill text:

```bash
curl -L https://raw.githubusercontent.com/dngrtech/minqlx-plugin-review/main/minqlx-plugin-review/SKILL.md -o GEMINI.md
```

If the project already has a `GEMINI.md`, paste the skill text into the existing file instead of overwriting it.

Then start Gemini CLI in that project:

```bash
gemini
```

Ask Gemini to review, write, or debug a minqlx plugin. The `GEMINI.md` context will be loaded for that project.

### Personal reusable slash command

For a global `/minqlx-plugin-review` command, create a Gemini CLI custom command:

```bash
mkdir -p ~/.gemini/commands
cat > ~/.gemini/commands/minqlx-plugin-review.toml <<'EOF'
description = "Review, write, or debug minqlx/minqlxtended plugins for Quake Live server safety."
prompt = """
Use this minqlx plugin review checklist:
!{curl -L https://raw.githubusercontent.com/dngrtech/minqlx-plugin-review/main/minqlx-plugin-review/SKILL.md}

Review, write, or debug the minqlx or minqlxtended plugin requested by the user.
Focus on frame/thread safety, hot-hook performance, HTTP/Redis batching, reload-safe workers, output flood control, stale Player objects, console-command injection, and cross-runtime API differences.

User request: {{args}}
"""
EOF
```

Then run it inside Gemini CLI:

```text
/minqlx-plugin-review review this plugin for lag spikes
```

## Installation (Claude Desktop)

Claude Desktop users can install this skill entirely from the app UI.

1. Open [`minqlx-plugin-review/SKILL.md`](minqlx-plugin-review/SKILL.md) on GitHub.
2. Copy the full contents of the file.
3. In Claude Desktop, open **Settings → Capabilities** and make sure **Skills** and **Code execution and file creation** are enabled.
4. Open **Customize → Skills**.
5. Create a new custom skill.
6. Name it `minqlx-plugin-review`.
7. Paste the copied `SKILL.md` text into the skill instructions/content field.
8. Save the skill and make sure it is enabled.

Start a new Claude chat and ask it to review, write, or debug a minqlx plugin. Claude should load the skill automatically when the request is relevant.

## Installation (Gemini Web Gems)

Gemini Web users can install this as a custom Gem from the browser UI.

1. Open [`minqlx-plugin-review/SKILL.md`](minqlx-plugin-review/SKILL.md) on GitHub.
2. Copy the full contents of the file.
3. Open [Gemini](https://gemini.google.com/).
4. Open **Explore Gems** / **Gem manager**.
5. Create a new Gem.
6. Name it `minqlx-plugin-review`.
7. Paste the copied `SKILL.md` text into the Gem instructions.
8. Save the Gem.

Start a new chat with that Gem and ask it to review, write, or debug a minqlx plugin.

## Verifying it loaded

In a session, ask:

> Use the minqlx-plugin-review skill to check this plugin for frame-safety issues.

The agent should announce it's using the skill and apply the checklist.

## License

MIT — see [LICENSE](LICENSE).

