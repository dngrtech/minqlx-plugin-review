---
name: minqlx-plugin-review
description: Use when reviewing, writing, or debugging a minqlx plugin for Quake Live server. Covers frame injection safety, thread boundaries, rate limiting player commands, and batch vs per-event HTTP patterns that cause gameplay spikes.
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

1. **Search for `requests.get` / `requests.post`** — every occurrence must be inside a `@minqlx.thread` decorated function.
2. **Search for `.put(` / `.kick(` / `.mute(` / `.health`** — every occurrence inside a `@minqlx.thread` function must be wrapped in `@minqlx.next_frame`.
3. **Search for `@minqlx.next_frame`** — every decorated function body should be trivial (a msg, a tell, a simple assignment). Flag anything that calls `add_request()` or starts more network work.
4. **Check all player-facing commands that call `fetch_*` or `add_request`** — verify a cooldown guard exists before the call.
5. **Check event handlers** (`handle_kill`, `handle_player_connect`, `handle_round_start`) — verify none contain blocking calls or complex synchronous logic.
6. **Search for redis calls** (`.zadd(`, `.hmset(`, version-sensitive APIs) — verify each goes through a try/except compat shim, not a direct call assuming one `redis-py` major version.
