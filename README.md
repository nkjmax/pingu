# Pingu — architecture

Single Discord bot process. TF2 competitive pug/mix/scrim hosting for one private community.

## Design principles

1. **One process, one bot token.** No Redis, no Docker, no multi-process — except Pingu Broadcast, which stays a separate small process for permission/blast-radius isolation.
2. **Layering is one-directional:** `cogs` (Discord entry points) → `views` (buttons/modals, thin) → `services` (business logic, no Discord calls) → `db` (SQLite access, one file per table). Nothing skips a layer. Services never touch Discord directly; only `views` and `ui_updater` do.
3. **One edit point.** Every message edit goes through `ui_updater.schedule_refresh()`, which debounces ~300ms and performs one combined edit instead of one edit per action.
4. **Constraints over app-level locks.** Fresh pug's "only one active" rule is a DB unique constraint, not an in-memory flag or Redis key.
5. **Proposals, not direct writes.** A captain never writes to `signups` directly — they write to `roster_proposals`, and the hoster's approval is what commits it.

## Match type gating

| Type | Who creates it | What blocks duplicates |
|---|---|---|
| Mix | Anyone submits a host request; a hoster approves it | Hoster approval is the gate |
| oPUG | Hoster role only, created directly | Role check is the gate |
| Fresh PUG | Anyone, created directly | DB unique constraint: one `status='live'` row per fresh-pug type at a time |

## Directory structure

```
pingu/
├── main.py
├── config.py
├── cogs/
│   ├── hosting.py
│   ├── fresh_pug.py
│   ├── opug.py
│   ├── manage.py
│   ├── roster.py
│   ├── linking.py
│   ├── moderation.py
│   ├── tickets.py
│   └── admin.py
├── views/
│   ├── hosting_views.py
│   ├── roster_views.py
│   ├── fresh_pug_views.py
│   └── manage_views.py
├── services/
│   ├── hosting_service.py
│   ├── fresh_pug_service.py
│   ├── roster_service.py
│   ├── archive_service.py
│   ├── log_service.py
│   ├── moderation_service.py
│   └── channel_service.py
├── ui/
│   └── ui_updater.py
├── db/
│   ├── __init__.py
│   ├── matches.py
│   ├── signups.py
│   ├── host_requests.py
│   ├── roster_proposals.py
│   ├── players.py
│   ├── match_logs.py
│   ├── penalties.py
│   ├── tickets.py
│   └── guild_settings.py
├── embeds.py
└── scheduler.py

pingu_broadcast/          # separate process, reads pingu's db read-only
└── main.py
```

## cogs/ — Discord entry points

Each cog owns slash commands and listeners for one feature area. Cogs call services; they never touch `db` directly.

- **hosting.py** — `/host` (submit a request: team name, division, map, server pref). Hoster-facing approve/deny buttons live in `hosting_views.py`, wired here.
- **fresh_pug.py** — `/freshpug` (create — open to anyone, rejected if one's already live), signup/leave buttons.
- **opug.py** — hoster-only creation commands. Unchanged from current behaviour.
- **manage.py** — `/manage` panel for a match's hoster (existing `ManageCog`, thinned to call services instead of `db` directly).
- **roster.py** — captain "propose pick" commands/buttons, hoster "approve/reject" buttons.
- **linking.py** — `/link <logs.tf profile url>` — stores the player's SteamID64.
- **moderation.py** — `on_message` listener for flagged content; `/penalize` command for mods.
- **tickets.py** — ban/report/suggestion ticket commands.
- **admin.py** — `/setup` — lets admins set channel IDs, roles, thresholds into `guild_settings`.

## views/ — UI only

Discord `View`/`Modal` classes. Read state, call a service method, done. No business logic, no direct `db` calls.

- **hosting_views.py** — `HostRequestApproveView`, `HostRequestDenyButton`.
- **roster_views.py** — `ProposePickView`, `ApproveProposalView`.
- **fresh_pug_views.py** — `FreshPugSignupView`, `FreshPugManageView`.
- **manage_views.py** — existing manage-panel views, ported as-is.

## services/ — business logic

No Discord API calls except through `ui_updater`. This is what's unit-testable.

- **hosting_service.py**
  - `submit_request(requester_id, team_name, division, map_name, server) -> host_request_id`
  - `approve_request(host_request_id, hoster_id) -> match_id` — creates the match, makes requester captain
  - `deny_request(host_request_id, hoster_id, reason)`

- **fresh_pug_service.py**
  - `create(creator_id, maps, server) -> match_id | RejectedAlreadyActive`
  - `join(match_id, user_id)` / `leave(match_id, user_id)`
  - `try_launch(match_id)` — checks fill, flips status to `live`

- **roster_service.py**
  - `propose_pick(match_id, captain_id, target_user_id, class_name) -> proposal_id`
  - `approve_proposal(proposal_id, hoster_id)` — commits to `signups`
  - `reject_proposal(proposal_id, hoster_id)`

- **archive_service.py**
  - `conclude(match_id, triggered_by)` — orchestrates: pulls roster, calls `log_service`, builds summary, posts to archive channel, tears down channels via `channel_service`
  - `cancel(match_id, triggered_by)`

- **log_service.py**
  - `find_candidate_logs(match_id) -> list[LogCandidate]` — queries logs.tf by roster SteamIDs, scores by roster-overlap within the match's time window
  - `fetch_log_stats(log_id) -> {score, damage_by_team, map_name}`

- **moderation_service.py**
  - `handle_violation(message)` — auto-warn, log to `penalties`/mod channel
  - `apply_penalty(user_id, type, duration, issued_by)`
  - `expire_penalties()` — called by scheduler sweep

- **channel_service.py**
  - `create_match_channels(match_id) -> category_id` — text + VC(s), scoped permission overwrites
  - `teardown_match_channels(match_id)`

## ui/ui_updater.py

- `schedule_refresh(match_id)` — enqueues a refresh, debounced ~300ms
- Internal loop performs one combined edit (main embed, pending list, ongoing-matches line) per flush instead of one edit per signup action

## db/ — one file per table, plain async functions

- **matches.py** — `matches` table: `id, type, status, map_name, timestamp, channel_id, category_id, host_request_id`. Fresh-pug singleton enforced here via partial unique index.
- **signups.py** — `signups` table: `id, match_id, user_id, class_name, status`.
- **host_requests.py** — `host_requests` table: `id, requester_id, status, notes`.
- **roster_proposals.py** — `roster_proposals` table: `id, match_id, captain_id, target_user_id, status`.
- **players.py** — `players` table: `user_id, steamid64, logs_tf_profile`.
- **match_logs.py** — `match_logs` table: `id, match_id, logs_tf_url, score_red, score_blu` (one-to-many — multi-map mixes get multiple rows).
- **penalties.py** — `penalties` table: `id, user_id, type, expires_at`.
- **tickets.py** — `tickets` table: `id, user_id, type, status, related_match_id`.
- **guild_settings.py** — channel/role IDs, thresholds, instead of hardcoding.

## scheduler.py

Existing jobs (unchanged): `clean_cancel_notices`, `clean_conclude_notices`, `send_1h_reminders`, `send_8h_reminders`, `re_sort`.
New job: `expire_penalties` sweep (removes ban/low-prio roles once `expires_at` passes).

## pingu_broadcast/

Separate bot process and token. Reads the same SQLite file (read-only) to post approved promotions/mix announcements into other servers. Not part of the layered structure above — deliberately isolated.

## Changelog from initial scaffold

- `/host` stays open to everyone (mix requests). Hoster-only `/opug` direct-create is not yet built — see `cogs/opug.py`. True per-role command hiding needs to be configured in Discord's Server Settings > Integrations once it exists (set `default_member_permissions=Permissions.none()` on it), not something the bot can declare alone.
- `/propose` and `/conclude` removed as standalone commands. Both now happen through `/manage`, which branches by caller: the match's `captain_id` sees `CaptainReviewView` (screen incoming signups), a hoster sees `ManageActionsView` (review picks / conclude / cancel).
- `signups.status` gained an `awaiting_hoster` state: captain accepts a signup -> limbo -> hoster gives final accept/deny, with an "accept all" shortcut.
- `matches.captain_id` added, set to the original requester on host-request approval — distinct from `created_by` (the approving hoster).
- `db/roster_proposals.py` is now unused (superseded by the signup-status lifecycle above) — left in place, harmless, safe to delete later.
- `/link` renamed to `/verify`, same behavior.
- `/penalize` and `/setup` held off — not loaded in `main.py`. `config.py` now reads settings straight from environment variables (see `.env.example`) instead of the `/setup` + `guild_settings` DB path.
- `/ticket` held off — `cogs/tickets.py` not loaded, pending more design.

## /host-request redesign

- `/host-request` is now the single entry point for non-hosters, replacing the earlier open `/host`. Running it shows two buttons: **Fresh PUG** (opens a small modal for maps/server, same singleton-enforced creation as before) and **Request a mix** (opens a modal for team/division/map/server).
- Your original hoster-only `/host` (direct mix creation, no request step) is untouched — it's not part of this scaffold at all; see `cogs/hosting.py` for the pointer back to your existing `schedule.py`.
- On "Request a mix" submit: a `host_requests` row is created, a thread is opened under `MIX_REQUESTS_CHANNEL_ID`, and the requester gets an ephemeral prompt to go ping their team in that thread.
- A message listener in `cogs/host_request.py` watches that thread: once the requester's ping message lands, it saves the roster, posts a summary, and pings the hoster role.
- A hoster runs `/manage` **inside that thread** — `cogs/manage.py` now checks "is this a pending, roster-filled mix-request thread" before falling back to the usual match-channel captain/hoster branching. Accept creates the match + channels via the existing `hosting_service.approve_request()` / `channel_service.create_match_channels()`, and assigns `CAPTAIN_ROLE_ID` if you've set one (in addition to the DB `captain_id`, which is what actually gates the captain's `/manage` view — the role is optional/cosmetic on top). Deny just marks it denied and pings the requester in-thread.
- Threads auto-delete 24h after resolution via a new `close_expired_request_threads` scheduler job. Requests nobody ever actions are a known gap — their thread stays open indefinitely; not handled yet.
- `/freshpug` no longer exists standalone — only reachable via `/host-request`.

## Full port completion notes

All 7 original files ported faithfully, verified both by `py_compile` and by
a real import-trace through the actual `pingu` package (not just syntax
checking):

| Original | Ported to | Lines |
|---|---|---|
| `db.py` | `db/matches.py`, `db/signups.py`, `db/__init__.py` | ~50 functions, every table/column preserved |
| `embeds.py` | `embeds.py` | 833 lines, every constant/emoji/channel ID kept |
| `main.py` | `main.py` | Groq chatbot persona + `_pending_roster` message handler preserved |
| `scheduler.py` | `scheduler.py` | all 5 original jobs, correct 1h/8h reminder semantics |
| `manage.py` | `cogs/manage.py` | `cog_load` persistent-view registration, type branching |
| `schedule.py` | `cogs/hosting.py` | 1,574 lines, full wizard + `/edit`, `/connect-string`, `/ping` |
| `views.py` | `views/legacy.py` | 2,442 lines, every view/button/select class |

**Config:** original `config.json` + `bot.config.get("key")` pattern replaced
with `config.py` module attributes, sourced from `.env`. Every key the
original used was tracked down during the port (some only surface deep in
schedule.py/views.py) — see `.env.example` for the full list, including the
structured ones (`MIX_CHANNELS` as comma-separated, `OPUG_CHANNELS`/`PING_ROLES`
as multiple flat vars reassembled into dicts).

**Your existing `matches.db`** will work as-is — `db/__init__.py` keeps the
original filename and migrates new columns onto the existing tables via
`ALTER TABLE`, same pattern the original bot used for its own migrations.

**Deliberate deviations from the original**, all additive:
- `matches.captain_id`, `category_id`, `host_request_id` — new columns for the mix-request flow
- `signups.status` gained `awaiting_hoster` — captain-screens-then-hoster-confirms lifecycle
- `do_archive`/`fire_archive_task` gained an optional `matched_logs` param — logs.tf score/damage/links, wired into `ConcludeConfirmView` and `FreshPugConcludeConfirmView`
- New tables (`host_requests`, `players`, `match_logs`, `penalties`, `tickets`, `guild_settings`) sit alongside the original two, untouched

Not ported (per earlier decisions in this conversation): `/penalize`, `/setup`,
`/ticket` — held off, not loaded in `main.py`'s `COGS` list.
