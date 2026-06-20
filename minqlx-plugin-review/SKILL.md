---
name: minqlx-plugin-review
description: Use when reviewing, writing, or debugging minqlx plugins. Covers frame/thread safety, hot-hook performance, HTTP/Redis batching, reload-safe workers, output flood control, and compatibility.
---

# minqlx Plugin Review

## Overview

minqlx plugins run inside the Quake Live dedicated server process. Any work done at the wrong time or in the wrong context causes visible gameplay spikes (hitches, lag). The server game loop runs at **40 fps (~25ms per frame)** — blocking or flooding the frame causes real player-facing impact.

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

Hooks that can fire many times per second must do almost nothing inline: `kill`, `death`, `damage`, `chat`, `client_command`, `team_switch`, `userinfo`, and ZMQ `stats` handlers.

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
