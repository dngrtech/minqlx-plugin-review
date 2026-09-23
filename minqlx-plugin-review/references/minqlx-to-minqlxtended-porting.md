# Offline minqlx → minqlxtended porting reference

Use this reference when converting a plugin from **minqlx to minqlxtended** and the agent cannot inspect a local minqlxtended checkout. It is a frozen, source-derived conversion contract, not a vague suggestion to read upstream documentation.

## Version contract

This matrix was checked against:

- minqlx upstream commit `fbdd915`;
- minqlxtended `v1.1.0-5-ga3de947`.

It is suitable for the normal source port between those API generations. If the target server identifies a materially newer or forked minqlxtended build, make the port using this reference but label the result **unverified against the target engine**. Do not pretend that a frozen table proves a future engine's API.

This document is directional. `minqlxtended → minqlx` is not a mechanical inverse: Extended-only events and APIs must be removed, emulated, or redesigned according to the plugin's intended behaviour.

## Conversion procedure

1. Preserve the original plugin as the source of truth; write the converted plugin separately until it imports and registers.
2. Convert every import before changing call sites. Expand wildcard imports; never carry `from minqlx import *` into the port.
3. Replace only Python **identifier tokens** `minqlx` with `minqlxtended`. Do not replace string literals, comments, Redis key prefixes such as `minqlx:players:*`, filenames, or Workshop metadata.
4. Inventory **all** hook registrations. Convert each changed event using the table below; for unchanged or dynamic registrations, record the expected signature or mark it for runtime-registration verification. In minqlxtended, one stale handler prevents the entire plugin from loading.
5. Convert all old `RET_*`, `PRI_*`, and `WP_*` identifiers to enum members. Search every branch, not only paths exercised in a smoke test.
6. Replace removed functions using the pinned mapping below. Do not retain a compatibility wrapper that calls an absent engine function.
7. Convert score reads to `Game.team_scores` indexed by `Team.index`; never unpack it as a red/blue pair.
8. Keep Redis key names unchanged unless a deliberate data migration accompanies the port.
9. Run the offline checker and manual review steps in this document, then run an import/registration smoke test on the target server or a matching test runtime before declaring success.

## Mechanical API changes

### Imports, namespace, and decorators

```python
# before
import minqlx

class example(minqlx.Plugin):
    @minqlx.thread
    @minqlx.next_frame
    @minqlx.delay(1)

# after
import minqlxtended

class example(minqlxtended.Plugin):
    @minqlxtended.thread
    @minqlxtended.next_frame
    @minqlxtended.delay(1)
```

Apply the same namespace conversion to `Plugin`, `NonexistentPlayerError`, `NonexistentGameError`, `console_command`, `get_cvar`, `set_cvar`, `hook`, `command`, and all other engine-facing names. A bare `minqlx` NAME token is a defect; a literal containing `minqlx` is not necessarily one.

For direct imports, preserve only supported names explicitly:

```python
# before
from minqlx import Plugin, RET_STOP_ALL, PRI_LOWEST

# after
from minqlxtended import Plugin, Return, Priority
# RET_STOP_ALL -> Return.STOP_ALL; PRI_LOWEST -> Priority.LOWEST at every use
```

Reject wildcard imports and direct imports of `RET_*`, `PRI_*`, or `WP_*`. `from minqlxtended import RET_STOP_ALL` is invalid; the target deliberately exposes enums instead.

### Return, priority, and weapon constants

minqlxtended intentionally does **not** export the old integer constant spellings. Convert each family:

| minqlx form | minqlxtended form |
|---|---|
| `minqlx.RET_NONE` | `minqlxtended.Return.NONE` |
| `minqlx.RET_STOP` | `minqlxtended.Return.STOP` |
| `minqlx.RET_STOP_EVENT` | `minqlxtended.Return.STOP_EVENT` |
| `minqlx.RET_STOP_ALL` | `minqlxtended.Return.STOP_ALL` |
| `minqlx.PRI_*` | `minqlxtended.Priority.*` |
| `minqlx.WP_*` | `minqlxtended.Weapon.*` |

Do not replace only `RET_STOP_ALL`: any surviving `RET_`, `PRI_`, or `WP_` identifier is a latent `AttributeError` or a bad comparison.

### Changed hook signatures

Only these existing events need handler changes. The signatures below are what the **handler** receives.

| Event | minqlx handler | minqlxtended handler | Port action |
|---|---|---|---|
| `player_connect` | `(player)` | `(player, is_bot)` | Add `is_bot`; use `_is_bot` only if intentionally ignored. |
| `game_start` | `(data)` | `()` | Remove `data`; recover required state from `self.game` or redesign. |
| `round_end` | `(data)` | `(round_number, winning_team, time)` | Replace stats-dict reads with these values or another appropriate API. |
| `team_switch_attempt` | `(player, old_team, new_team)` | `(player, old_team, new_team, target)` | Add `target`. |
| `chat` | `(player, msg, channel)` | `(player, msg, channel, recipient)` | Add `recipient`, even if it is unused. |
| `userinfo` | `(player, changed)` | `(player, changed, infostring)` | Add `infostring`, even if it is unused. |
| `game_end` | `(data)` | `(aborted)` | This is a semantic change: do not index `aborted` as a stats dict. |
| `kill` | `(victim, killer, data)` | `(victim, killer, mod)` | This is a semantic change: `mod` is not a stats dict. |
| `death` | `(victim, killer, data)` | `(victim, killer, mod)` | This is a semantic change: `mod` is not a stats dict. |

**False alarms:** leave `vote_started(caller, vote, args)` and `vote_ended(votes, vote, args, passed)` unchanged. minqlx declares different `dispatch()` parameters internally but forwards the same handler contracts.

For every unchanged static registration, preserve its handler arity. For a dynamic name, alias, helper, generated callback, or any registration the checker cannot resolve, leave a migration note and require target-runtime registration verification; do not mark it fully statically verified.

### Extended-only events

These have no minqlx equivalent and need no conversion when going toward minqlxtended:

- `cvar_changed(name, old_value, new_value)`
- `damage(target, attacker, damage, dflags, mod)`
- `demo_finished(client_id, path, size, discarded, failed)`
- `item_pickup(player, item_name)`
- `objective(player, kind, count)`
- `weapon_fired(player, weapon)`

Do not invent handlers for these during a mechanical port. `damage` and `weapon_fired` are hot hooks and require a separate performance review.

### Removed engine functions

These minqlx functions are absent from minqlxtended. The table is the pinned `v1.1.0-5-ga3de947` replacement contract; resolve the same player target before using the replacement.

| Removed minqlx function | minqlxtended replacement | Value shape / caveat |
|---|---|---|
| `set_ammo` | `player.ammo = value` | `Weapons` collection |
| `set_armor` | `player.armor = value` | integer |
| `set_flight` | `player.flight = value` | `Flight`; setting grants flight holdable if needed |
| `set_health` | `player.health = value` | integer |
| `set_holdable` | `player.holdable = value` | `Holdable` or `None` |
| `set_invulnerability` | `player.invulnerability(time)` | **method**, not property assignment |
| `set_position` | `player.position = value` | `Vector3` |
| `set_powerups` | `player.powerups = value` | `Powerups` collection |
| `set_privileges` | `player.privileges = value` | `Privilege` or `None` |
| `set_score` | `player.score = value` | integer |
| `set_velocity` | `player.velocity = value` | `Vector3` |
| `set_weapon` | `player.weapon = value` | `Weapon` enum |
| `set_weapons` | `player.weapons = value` | `Weapons` collection |
| `noclip` | `player.noclip = enabled` | boolean property |
| `allow_single_player` | no pinned direct equivalent | leave a manual migration blocker; redesign only against the target runtime |

`Plugin.kick(...)` is also gone. `Player.kick(reason)` survives. If the old code invokes `self.kick(...)`, resolve the player and call the player method instead; preserve the original target-selection and reason logic.

### Scores

```python
# before
red = self.game.red_score
blue = self.game.blue_score

# after
scores = self.game.team_scores
red = scores[minqlxtended.Team.RED.index]
blue = scores[minqlxtended.Team.BLUE.index]
```

`team_scores` has four positions. Do not unpack it into two values. In free-for-all, configstring score slots mean first/second place, not red/blue teams.

## Offline acceptance checks

Run the checked-in stdlib-only scanner over the converted plugin directory:

```bash
python3 /path/to/minqlx-plugin-review/minqlx-plugin-review/scripts/check_minqlxtended_port.py /path/to/converted-plugin
```

It reports file/line violations for old namespaces, legacy constants, legacy direct imports, wildcard imports, removed APIs, score fields, and supported static hook forms. Its hook inventory is deliberately conservative: it reports dynamic registrations as manual-review items rather than pretending to understand arbitrary Python.

Then perform these manual checks:

1. For every static hook inventory entry, verify changed events against the table and unchanged events retain their known handler arity.
2. For every dynamic/manual hook entry, record the handler contract or mark it **requires target-runtime registration verification**.
3. Review every `game_end`, `kill`, `death`, and `round_end` conversion; their similar arity hides semantic changes.
4. Review every removed-API match against the mapping table. An `allow_single_player` match is a blocker, not a completed port.
5. `py_compile` is syntax-only: it does **not** import `minqlxtended`, resolve attributes, or register hooks. A matching runtime must import and instantiate the plugin before success can be claimed.

## What this cannot prove

Without the target engine, this reference cannot prove that the plugin registers, imports, or preserves runtime behaviour on a particular server build. It provides a bounded, source-derived conversion baseline. A matching minqlxtended runtime remains the final verification path; no amount of Markdown can summon an absent binary out of the fucking void.
