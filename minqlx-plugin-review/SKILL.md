---
name: minqlx-plugin-review
description: Use when reviewing, writing, or debugging minqlx or minqlxtended plugins. Covers frame/thread safety, hot-hook performance, HTTP/Redis batching, reload-safe workers, output flood control, console-command injection, and cross-runtime API differences.
---

# minqlx Plugin Review

## Overview

minqlx plugins run inside the Quake Live dedicated server process. Any work done at the wrong time or in the wrong context causes visible gameplay spikes (hitches, lag). The server game loop runs at **40 fps (~25ms per frame)** — blocking or flooding the frame causes real player-facing impact.

## First: which runtime?

There are two hard forks and **plugins are not portable between them**. Establish which one you are reviewing before anything else — check the imports.

| | minqlx | minqlxtended |
|---|---|---|
| Import | `import minqlx` | `import minqlxtended` |
| Stop a hook | `minqlx.RET_STOP_ALL` | `minqlxtended.Return.STOP_ALL` |
| Priority | `minqlx.PRI_LOWEST` | `minqlxtended.Priority.LOWEST` |
| Bad handler signature | fails at dispatch, on that path | **refused at registration — plugin does not load** |

Everything below applies to both unless marked otherwise. The API deltas are in
**minqlxtended Differences** near the end; read that section too if the plugin
imports `minqlxtended`.

---

## Execution Contexts

| Decorator | Runs in | Safe for game state? |
|-----------|---------|----------------------|
| `@minqlx.thread` | Background OS thread | **No** — reads OK, writes forbidden |
| `@minqlx.next_frame` | Next game tick | **Yes** — all game mutations go here |
| `@minqlx.delay(N)` | Game tick N seconds later | **Yes** |
| Plain handler | Game tick (event callback) | **Yes** |

**The one rule:** Any mutation of game state (`.put()`, `.kick()`, `.mute()`, `.position()`, `.velocity()`, `.health`) from inside a `@minqlx.thread` MUST be wrapped in a `@minqlx.next_frame` inner function.

```python
# ❌ WRONG — game mutation directly in thread
@minqlx.thread
def do_thing():
    player.put("spectator")   # crashes or silently corrupts state

# ✅ CORRECT — mutation queued into game frame
@minqlx.thread
def do_thing():
    @minqlx.next_frame
    def execute():
        player.put("spectator")
    execute()
```

---

## Review Checklist

### 1. Frame injection is minimal
`@minqlx.next_frame` should inject the smallest possible unit of work — ideally a single `self.msg()` or `player.tell()`. If the callback does API calls, team lookups, or re-triggers more fetches, that is a spike source.

**Red flag:** A `@minqlx.next_frame` callback that calls `add_request()` or `fetch_*()` again (cascading re-fetch). This is the worst pattern — an HTTP response lands in the game frame, then immediately kicks off another HTTP thread, which injects another callback, repeating until all players are covered.

```python
# ❌ balance.py anti-pattern — cascading re-fetch in frame callback
@minqlx.next_frame
def handle_ratings_fetched(self, request_id, status_code):
    ...
    callback(players, channel, *args)   # callback may call add_request() again

# ✅ ranked.py pattern — only a msg() injected back
@minqlx.thread
def fetch():
    ...build output string...
    @minqlx.next_frame
    def send(m=out):
        self.msg(m)
    send()
```

### 2. Player-triggered commands have a cooldown

Any command that fans out into HTTP (`!elo`, `!teams`, `!balance`, `!rank`) must be rate-limited. Without a cooldown, 10 players typing `!elo` simultaneously spawns 10 HTTP threads, all of which inject callbacks into the same frame window.

```python
ELO_LIST_COOLDOWN = 10  # seconds

def cmd_elos(self, player, msg, channel):
    now = time.time()
    remaining = ELO_LIST_COOLDOWN - (now - self.last_elo_list_time)
    if remaining > 0:
        player.tell(f"^1On cooldown. Wait {int(remaining) + 1}s.")
        return minqlx.RET_STOP_ALL
    self.last_elo_list_time = now
    # ... proceed with fetch
```

### 3. HTTP is always in a thread, never in a handler

A network call in an event handler or plain function blocks the game loop for the entire RTT (can be 50–500ms for external APIs). That is a guaranteed spike.

```python
# ❌ blocks game loop
def handle_player_connect(self, player):
    r = requests.get("http://qlstats.net/elo/{}".format(player.steam_id))

# ✅ offloaded
def handle_player_connect(self, player):
    self._fetch_and_check(player)

@minqlx.thread
def _fetch_and_check(self, player):
    r = requests.get("http://qlstats.net/elo/{}".format(player.steam_id))
    ...
```

### 4. Kill/event data is batched, not sent per-event

Per-kill HTTP calls multiply linearly with server activity. In an FFA game with 16 players that's potentially hundreds of requests per minute. Accumulate events in memory using a `threading.Lock()` and flush on a timer.

```python
# ✅ ranked.py batch pattern
def handle_kill(self, victim, killer, kwargs):
    with self._lock:
        self._kill_events.append({...})   # just append, no network

@minqlx.thread
def _batch_timer():
    while True:
        time.sleep(interval)
        self._submit_batch()              # one HTTP call per interval
```

### 5. Position/stat polling never writes game state

Background polling threads that read `player.position()` or `player.stats` are fine — reads are safe from threads. Problems only occur if the polling thread then tries to write (teleport, slay, etc.) without going through `@minqlx.next_frame`.

```python
@minqlx.thread
def poll():
    while True:
        time.sleep(1)
        for player in self.players():
            pos = player.position()       # ✅ read is safe
            self._last_pos[sid] = (pos.x, pos.y, pos.z)  # ✅ dict write, not game state
```

### 6. Prefer localhost services over external APIs

A thread blocked on `qlstats.net` waits 100–500ms before it can inject its `@minqlx.next_frame` callback. A thread hitting `localhost:5002` resolves in <1ms. If you control the backend, serve it locally and drop the external dependency.

### 7. Threaded HTTP still has timeouts, cache, and single-flight

Moving HTTP into `@minqlx.thread` only protects the game frame from direct blocking. It does not protect the server from leaked/hung worker threads, stale callbacks, duplicate requests, or API storms.

Every `requests.get()` / `requests.post()` must have a finite timeout. Rating/profile lookups should have a short TTL cache and deduplicate identical in-flight requests (`steam_id`, gametype, endpoint) so ten players cannot trigger ten copies of the same fetch.

```python
try:
    res = requests.get(url, timeout=(1.0, 3.0))
    res.raise_for_status()
    data = res.json()
except (requests.RequestException, ValueError) as e:
    minqlx.get_logger().warning("rating fetch failed: %s", e)
    return
```

Red flags: `requests.get(url)` with no `timeout`, unbounded retries, no JSON validation, update/version checks on connect or load, duplicate concurrent fetches for the same player data.

### 8. Hot hooks are O(1), append-only, or offloaded

Hooks that can fire many times per second must do almost nothing inline: `frame`, `kill`, `death`, `damage`, `chat`, `client_command`, `team_switch`, `userinfo`, and ZMQ `stats` handlers.

**`frame` is the hottest of all and the easiest to underestimate** — it fires every single game tick, 40 times a second, unconditionally, whether or not anything is happening. It is not event-driven; there is no quiet server. A `frame` handler must self-throttle on its very first lines, before it touches players, cvars, or the DB:

```python
# ✅ highfps.py pattern — count frames, do real work once per interval
def handle_frame(self):
    self.frame_counter += 1
    sv_fps = int(self.get_cvar("sv_fps") or "40")
    if self.frame_counter < sv_fps * self._get_sample_interval():
        return
    self.frame_counter = 0
    ...actual work...
```

Note it derives the budget from `sv_fps` rather than hardcoding 40 — a host running a different tick rate would otherwise sample at the wrong interval.

That example is the right shape but not the floor: it still reads two cvars on every tick, ~80 engine calls a second, to compute a threshold that changes almost never. Caching them and refreshing on `cvar_changed` (or just on each interval boundary) is strictly better. Worth raising as a nit, not a defect.

Red flags in a `frame` handler: `self.players()` before the throttle check, **any** DB or Redis call, HTTP, per-player `time.monotonic()` bookkeeping every tick, or a throttle that hardcodes 40 instead of reading `sv_fps`.

`@minqlx.thread` inside a hot hook is not automatically safe. A thread per kill/chat line plus several Redis calls is still a thread storm and a DB round-trip storm.

Prefer: append to an in-memory queue under a lock, aggregate during the match, then flush with a pipeline at round/game end or on a coarse timer.

Red flags: DB reads/writes per kill, player loop × Redis call, thread per event, `self.msg()` per event, expensive regex/string parsing in chat hooks.

### 9. Long-running threads and recurring delays are reload-safe

`@minqlx.thread` creates daemon threads. Plugin reload does not magically stop custom `while True` loops, sockets, polling workers, or recurring `@minqlx.delay` callbacks. `@minqlx.delay` schedules into the global frame scheduler; unload removes hooks/commands, not already queued tasks.

Any persistent worker must have an unload hook and cancellation flag/event.

```python
def __init__(self):
    self.stop_event = threading.Event()
    self.add_hook("unload", self.handle_unload)
    self.worker()

def handle_unload(self, plugin):
    if plugin == self.__class__.__name__:
        self.stop_event.set()

@minqlx.thread
def worker(self):
    while not self.stop_event.wait(5):
        ...
```

Red flags: `while True` with `time.sleep()`, sockets/IRC/Discord clients without close/quit/join, recurring `@minqlx.delay` that reschedules forever without `self._alive`, mutable default args like `stop_event=threading.Event()`.

### 10. Re-resolve players and guard game state after delays/threads

`Player` objects are snapshots. A delayed or threaded callback can run after the player disconnected, changed teams, reused a client slot, or after a map restart. `@minqlx.next_frame` means "run on the game thread", not "the game and players are fully initialized".

Prefer storing `client_id + steam_id`, then re-resolving on the frame and verifying identity before mutation. `Plugin.player()` treats integers in `[0, 64)` as client slots, not SteamIDs, so SteamID-only lookup is unsafe for bots or any nonstandard low ID.

```python
client_id = player.id
sid = player.steam_id

@minqlx.next_frame
def apply():
    if not self.game:
        return
    try:
        p = self.player(client_id)
    except minqlx.NonexistentPlayerError:
        return
    if not p or p.steam_id != sid:
        return
    p.put("spectator")
```

Red flags: storing `Player` objects in long-lived dicts/lists, re-resolving by SteamID alone for bots/low IDs, using `self.game.type_short` without `if not self.game`, treating `player_connect` as fully loaded state, delayed callbacks that do not tolerate map changes.

### 11. Large output is capped, paginated, or throttled

Commands that list maps, players, bans, permissions, stats, DB rows, or generated text can flood client command buffers and frame work even if they do no network I/O.

Red flags: loops calling `player.tell()` / `channel.reply()` for every DB row, `self.msg()` inside player loops, recursive `@minqlx.next_frame` output without a hard line/byte cap.

Prefer pagination (`!cmd 1`, `!cmd 2`), summaries, private output for detail, and hard caps for public chat output.

### 12. Use hook return values and priorities deliberately

`RET_STOP`, `RET_STOP_EVENT`, and `RET_STOP_ALL` have different behavior. High-priority hooks and broad stop returns can block other plugins or engine behavior.

Red flags: unnecessary `PRI_HIGHEST`, `RET_STOP_ALL` in common hooks without a comment, command validation that sends an error but returns `None`, blocking `team_switch` after it happened instead of using `team_switch_attempt` where possible.

### 13. No import-time network, self-modifying downloads, or unsafe parsing

Plugin import and `__init__` should not depend on GitHub, DNS, or external services. Do not auto-download Python files into the plugin directory at runtime.

Never use `eval()` / `exec()` on cvars, chat input, DB values, or files. Use `json.loads`, `ast.literal_eval`, or an explicit parser and validate ranges.

Bare `except:` and `except Exception` in loops/import/export paths must log with context (`minqlx.log_exception()` or logger) and report skipped records/counts to admins. Silent failure is how corrupted state turns into archaeology.

### 14. Never interpolate untrusted input into `console_command()`

`console_command()` hands a string to the engine console, and **the console treats `;` as a command separator**. Any caller-controlled value formatted into it can append arbitrary console commands. This is a separate sink from `eval`/`exec` in §13 and is easy to miss, because the call looks like string formatting rather than code execution.

```python
# ❌ real pattern, found in the wild in myFun.py's !sound
minqlx.console_command("fdir {}".format(msg[1]))
#   !sound x; quit          -> runs `quit`
#   !sound x; rcon_password -> whatever the caller wants

# ✅ allow-list the shape the value is supposed to have, and reject on failure
SOUND_PATH_ALLOWED = re.compile(r"^[A-Za-z0-9_./-]+\Z")

def is_safe_sound_path(value):
    return bool(value) and bool(SOUND_PATH_ALLOWED.match(value))

if not is_safe_sound_path(msg[1]):
    player.tell("^1Invalid sound path.")
    return
minqlx.console_command("fdir {}".format(msg[1]))
```

Two things reviewers get wrong here:

- **A permission level is a mitigation, not a fix.** "It's `add_command(..., 3)`, only admins can call it" narrows who can reach it; it does not stop the command doing something other than what it says. A moderator asking for a sound should not be silently handed the whole console.
- **Anchor with `\Z`, not `$`.** In Python `$` also matches just before a *trailing newline*, so `"ok.ogg\n"` passes a `$`-anchored allow-list. Whether that is reachable depends on how the argument was tokenised — don't reason about it, just use `\Z`.

Validate at the boundary and reject, rather than trying to escape or strip. Same applies to any value reaching `set_cvar()` where the name or value is caller-supplied.

Red flags: `console_command(` or `set_cvar(` with a `.format(`, f-string, or `%` on anything derived from `msg[...]`, chat text, a cvar, or a DB value; an allow-list anchored with `$`; "it's permission-gated" offered as the reason no validation is needed.

---

## Redis / Storage Performance

minqlx uses Redis for its built-in DB. Compatibility shims are not enough: Redis can still be a runtime bottleneck when plugins scan the whole keyspace or perform many round trips in hot paths.

**Never use broad `KEYS` in runtime commands or hooks.** `self.db.keys("minqlx:players:*")` and similar patterns block Redis and scale with historical server data, not current player count.

Prefer:

- `scan_iter()` or maintained index sets/zsets for keyspace iteration.
- Pagination and hard result limits for admin commands.
- Redis pipelines for many related reads/writes.
- `mget` / `hmget` instead of per-player loops of `get` / `hget`.
- In-memory aggregation during a match and one DB flush at round/game end.
- Local Redis / UNIX socket when available.

Red flags: `db.keys("*")`, large `smembers` / `lrange` / `zrange` in chat commands, DB call inside a player loop in a hot hook, command output proportional to all historical players.

---

## Library / Environment Compatibility

minqlx runs against a **bundled Python whose `redis-py` version is not guaranteed**. Hosts have shipped `redis-py` 2.10.6, where several call signatures differ from 3.x+. This version lives inside minqlx's embedded interpreter on the game host — it is **not** pinned in the repo, docs, or any tool the reviewing agent can read. Do not try to detect it by inspecting the codebase; you won't find it there.

**The rule:** Never call version-sensitive redis APIs directly. Wrap them in a small try/except compat shim that attempts the modern signature and falls back to the legacy one. This is version-agnostic and strictly more robust than detection — it works on both without knowing which is installed.

```python
# ✅ canonical pattern — ban.py zadd_compat
def zadd_compat(db, key, member, score):
    """Support both redis-py 2.x (raises RedisError) and 3.x+ (dict mapping) zadd signatures."""
    try:
        return db.zadd(key, {member: score})       # redis-py 3.x+
    except (TypeError, _RedisError):
        return db.zadd(key, score, member)         # redis-py 2.x
```

Common 2.x↔3.x differences to shim: `zadd` (mapping dict vs. positional `score, member`), and `hmset` (deprecated/removed in favor of `hset` with a mapping).

**Ask the user for their `redis-py` version only as a last resort** — when no shim can bridge the difference (rare). For the overwhelming majority of cases the try/except shim removes the question entirely.

**On minqlxtended this concern mostly evaporates:** the engine sets `qlx_redisProtocol` to `3` by default (`_core.py`, `set_cvar_once("qlx_redisProtocol", "3")`), so RESP3 is in play and `database.py` reads that cvar when connecting. The modern `redis-py`/`hiredis` floors come from the plugins repo's `requirements.txt` (`minqlxtended-plugins`, which the engine README tells you to `pip install -r`) — not from the engine repo, which declares no Python dependencies at all. Check that file for the actual pins rather than assuming a version. Either way the 2.x signatures are very unlikely to be in play: existing shims are harmless and worth keeping in code shared between runtimes; don't write *new* ones for a minqlxtended-only plugin.

---

## minqlxtended Differences

Everything above applies to minqlxtended too. What follows is only what *differs*, verified against the engine source — not inferred. Upstream's own summary: *"Plugins written for minqlx won't load on this version without alteration."*

### Signatures are validated at registration, not at dispatch

This is the single most important difference. `add_hook()` inspects the handler's signature against the event's and raises immediately, so **one stale handler stops the entire plugin from loading at server start** rather than misbehaving later on one code path.

Practically, this is good news for review: failures are loud and early rather than latent. But it means a plugin with six correct handlers and one wrong one gives you *nothing* — not a partly-working plugin. When a ported plugin "doesn't load", check every handler signature before anything else.

### Eleven event signatures changed

Upstream's README calls out "the six events that used to come off the ZMQ stats feed" — those now come from the game module, so **`zmq_stats_enable 1` is no longer mandatory** for them. But six is the count of *that* group, not of all signature changes. Diffing every dispatcher in both runtimes gives **eleven**:

| Event | minqlx | minqlxtended |
|---|---|---|
| `player_connect` | `(player)` | `(player, is_bot)` |
| `game_start` | `(data)` | `()` |
| `game_end` | `(data)` — stats dict | `(aborted)` — a flag |
| `round_end` | `(data)` | `(round_number, winning_team, time)` |
| `team_switch_attempt` | `(player, old_team, new_team)` | `(player, old_team, new_team, target)` |
| `kill` / `death` | `(victim, killer, data)` — stats dict | `(victim, killer, mod)` — means of death |
| `chat` | `(player, msg, channel)` | `(player, msg, channel, recipient)` |
| `userinfo` | `(player, changed)` | `(player, changed, infostring)` |
| `vote_ended` | `(passed)` | `(votes, vote, args, passed)` |
| `vote_started` | `(vote, args)` | `(caller, vote, args)` |

The last four are easy to miss because they have nothing to do with ZMQ stats, and `chat` in particular is hooked by a large fraction of plugins in the wild. A 3-argument `chat` handler or a 1-argument `vote_ended` handler will not bind, so the plugin does not load at all.

Genuinely unchanged, safe to leave alone: `frame` `()`, `map` `(mapname, factory)`, `player_disconnect` `(player, reason)`, `player_loaded` `(player)`, `player_spawn` `(player)`, `console_print` `(text)`, `set_configstring` `(index, value)`, `round_start`/`round_countdown` `(round_number)`, `new_game` `()`, `team_switch` `(player, old_team, new_team)`, `client_command`/`server_command` `(player, cmd)`, `command` `(caller, command, args)`, `vote` `(player, yes)`, `vote_called` `(player, vote, args)`, `stats` `(stats)`, `unload` `(plugin)`, `game_countdown` `()`, `kamikaze_use` `(player)`, `kamikaze_explode` `(player, is_used_on_demand)`.

### Six events are new in minqlxtended

`cvar_changed` `(name, old_value, new_value)`, `damage` `(target, attacker, damage, dflags, mod)`, `demo_finished` `(client_id, path, size, discarded, failed)`, `item_pickup` `(player, item_name)`, `objective` `(player, kind, count)`, `weapon_fired` `(player, weapon)`.

These have no minqlx equivalent — 32 events there, 38 here. Nothing to port, but `damage` and `weapon_fired` are extremely hot (see §8), and a plugin newly hooking them is a performance review, not a porting one.

The authoritative list is `python/minqlxtended/_events.py` — each dispatcher class's `dispatch()` parameter list, minus `self`. Read it rather than guessing; don't trust this table over the source if the two ever disagree.

Watch for handlers that *silently* still work but now mean something else. `handle_game_end(self, data)` binds fine to `(aborted)` — one positional argument either way — so it registers, then reads `data["ABORTED"]` on a bool and raises at game end. `handle_vote_ended(self, passed)` is the same trap: it binds to `(votes, ...)` and reads a vote-count tuple as a boolean.

### Every constant family is an enum

`RET_STOP_ALL` → `Return.STOP_ALL`, `PRI_LOWEST` → `Priority.LOWEST`, `WP_RAILGUN` → `Weapon.RAILGUN`, and so on.

The old spellings are **deliberately excluded from the `minqlxtended` namespace**, not merely renamed — `minqlxtended.RET_STOP_ALL` raises `AttributeError`. That exclusion is load-bearing and worth understanding: the underlying C constant still exists, and if it were re-exported, `RET_STOP_ALL` would come back as the plain int `3`, which a handler can return and **have silently ignored**. Failing loudly at the attribute access is the point.

So a missed constant is an `AttributeError` at the moment a player triggers that path — long after load, and only on that path. This is the one class of porting error that registration-time validation does *not* catch, which makes a source-level sweep for `RET_`/`PRI_`/`WP_` worth doing explicitly.

### Single-field engine functions became property access

Setters that each wrote one field are gone in favour of direct property assignment on the object (`player.health = 100` rather than a `set_health()`-style call). If a port calls a function that no longer exists, that is an `AttributeError` on first use.

Upstream's README says "twenty-two engine functions". Take the number loosely — diffing the two C modules' exported functions gives **15** names that exist in minqlx and not in minqlxtended, and that list is what you actually grep for:

```
set_ammo      set_armor      set_flight     set_health     set_holdable
set_invulnerability          set_position   set_powerups   set_privileges
set_score     set_velocity   set_weapon     set_weapons
noclip        allow_single_player
```

(Upstream presumably counts something broader — removed methods elsewhere, or the properties that replaced them. The wiki's [Upgrading](https://github.com/tjone270/minqlxtended/wiki/Upgrading) page is cited as the full list.) Note `set_configstring`, `set_cvar` and `set_cvar_limit` survive on both runtimes — they are not single-field player setters, and §14 still applies to them.

Related trap: verify the method actually exists on the class you are calling it on. `self.kick(...)` works on minqlx — `Plugin.kick` is a `@classmethod` there (`_plugin.py:461`) — but it is **gone from `minqlxtended.Plugin`**, so a port carrying it over gets an `AttributeError` the first time it fires. It survives review easily because `Player.kick(reason)` does exist on both and reads the same at a glance.

### Game score API

`Game.red_score` / `Game.blue_score` are gone. `Game.team_scores` replaces them: a **4-tuple indexed by `Team.index`**, taken straight off the game module's per-team array — not a two-element red/blue pair, so index it, don't unpack it.

Carry over the docstring's caveat: `CS_SCORES1` / `CS_SCORES2` mean "1st place" and "2nd place", which in a free-for-all are *not* teams. A port that maps them onto red/blue will read plausible-looking nonsense in FFA rather than failing.

Guard the access: `Game` raises `NonexistentGameError` — a bare `Exception` subclass, deliberately *not* a `ValueError` or `AttributeError`, so a handler guarding only those will let it through. **This one is not a minqlxtended difference** — `NonexistentGameError` subclasses plain `Exception` on both runtimes, so the guard is worth checking in any plugin you review.

### Review checklist for a ported plugin

1. Every handler signature matches the event's current arity — this is pass/fail for the whole plugin.
2. No bare `minqlx` identifier survives. **Tokenise, don't grep**: these files legitimately contain the string `minqlx` in Redis key prefixes (`minqlx:players:...`, which are an app-level contract, not an API), Steam Workshop item titles, and docstrings. Only a NAME token is a module reference. Note also that `minqlx` is a prefix of `minqlxtended`, so a naive `grep minqlx` matches every correct line.
3. No `RET_` / `PRI_` / `WP_` constant survives anywhere, including branches that are hard to reach.
4. Redis key names kept as-is unless there is a reason to change them — they are usually a contract with something outside the plugin, and renaming buys nothing but a migration.

---

## Common Spikes and Their Cause

| Symptom | Likely cause |
|---------|-------------|
| Spike when players type `!elo` simultaneously | No cooldown, multiple HTTP threads injecting callbacks into same frames |
| Spike at round start | `handle_round_countdown` or `handle_round_start` doing team lookups + HTTP inline |
| Periodic spike every N minutes | Batch timer running heavy processing synchronously in game thread instead of `@minqlx.thread` |
| Spike on player connect | `handle_player_connect` making blocking HTTP call in handler |
| Cascading spam after shuffle vote | `callback_balance` re-triggering `add_request()` if roster changed during fetch |

---

## Quick Audit Steps

1. **Search for `requests.get` / `requests.post`** — every occurrence must be inside a `@minqlx.thread` decorated function and include a finite `timeout`.
2. **Search for `.put(` / `.kick(` / `.mute(` / `.health` / `self.msg` / `player.tell` / `console_command` / `set_cvar`** — every engine-facing write inside a `@minqlx.thread` function must be wrapped in `@minqlx.next_frame`.
3. **Search for `@minqlx.next_frame`** — every decorated function body should be trivial (a msg, a tell, a simple assignment). Flag anything that calls `add_request()`, starts more network work, loops over players, or emits unbounded output.
4. **Check all player-facing commands that call `fetch_*` or `add_request`** — verify a cooldown guard exists before the call, identical in-flight requests are deduplicated, and cached data has a TTL.
5. **Check hot event handlers** (`handle_kill`, `handle_death`, `handle_chat`, `handle_client_command`, `handle_team_switch`, `handle_stats`) — verify they are O(1), append-only, or offload work without spawning unbounded threads.
6. **Search for Redis scans and large reads** (`.keys(`, `.smembers(`, `.lrange(`, `.zrange(`, per-player `.get(`/`.hget(` loops) — verify they are paginated, pipelined, moved out of hot paths, or replaced with `scan_iter()` / indexes.
7. **Search for redis compat calls** (`.zadd(`, `.hmset(`, version-sensitive APIs) — verify each goes through a try/except compat shim, not a direct call assuming one `redis-py` major version.
8. **Search for `while True`, `time.sleep`, `threading.Thread`, sockets, and recurring `@minqlx.delay`** — verify unload cleanup, stop flags/events, and no stale callbacks after reload.
9. **Search for stored `Player` objects or per-player dicts** — verify re-resolution before delayed/threaded mutation, disconnect cleanup, map/game cleanup, and consistent `steam_id` key types.
10. **Search for large output loops** (`player.tell`, `channel.reply`, `self.msg`) — verify hard caps, pagination, or throttling.
11. **Search for `eval(` / `exec(` / bare `except:`** — reject unsafe parsing and require contextual logging/reporting for caught errors.
12. **Search for import-time network or code downloads** — reject `requests` calls and downloaded `.py` writes during import/`__init__` unless explicitly optional, timeout-bound, and failure-isolated.
13. **Search for `console_command(` and `set_cvar(`** — any `.format(`, f-string, or `%` on caller-derived input is a console-injection hole. A permission level is not a fix. Check allow-lists are anchored `\Z`, not `$`.
14. **Search for `add_hook("frame"`** — verify the handler self-throttles on its first lines, before touching players, cvars, or the DB, and derives its budget from `sv_fps` rather than hardcoding 40.
15. **If the plugin imports `minqlxtended`** — check every handler signature against `_events.py` (registration is pass/fail for the whole plugin), then sweep for surviving `RET_`/`PRI_`/`WP_` constants, `red_score`/`blue_score`, the 15 removed engine functions (`set_health`/`set_score`/`set_position`/… plus `noclip`, `allow_single_player`), and bare `minqlx` NAME tokens. Tokenise for that last one; `grep minqlx` also matches every correct `minqlxtended` line, and the string appears legitimately in Redis keys and docstrings.
