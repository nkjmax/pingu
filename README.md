# Pingu — architecture

Single Discord bot process. TF2 competitive pug/mix/scrim hosting for one private community.

## Design principles

1. **One process, one bot token.** No Redis, no Docker, no multi-process — except Pingu Broadcast, which stays a separate small process for permission/blast-radius isolation.
2. **Layering is one-directional:** `cogs` (Discord entry points) → `views` (buttons/modals, thin) → `services` (business logic, no Discord calls) → `db` (SQLite access, one file per table). Nothing skips a layer. Services never touch Discord directly; only `views` and `ui_updater` do.
3. **One edit point.** Every message edit goes through `ui_updater.schedule_refresh()`, which debounces ~300ms and performs one combined edit instead of one edit per action.
4. **Constraints over app-level locks.** Fresh pug's "only one active" rule is a DB unique index, not an in-memory flag.
5. **Proposals, not direct writes.** A captain never writes an accepted/denied signup directly — they set `signups.captain_decision` (`'accept'`/`'deny'`), and the hoster's own Approve/Reject action is what actually commits it via `roster_service.finalise_accept`/`finalise_deny`. Status stays `'pending'` the whole time a captain proposal is outstanding, so nothing changes for players until a hoster actually confirms it.

## Match type gating

| Type | Who creates it | What blocks duplicates |
|---|---|---|
| Mix | `/host` (hoster, direct) or `/host-request` → hoster approval (requester becomes captain) | Hoster role, or hoster approval |
| oPUG | `/host`, hoster role only | Role check |
| Fresh PUG | `/host` or `/host-request`, open to anyone | DB unique index: one `ended=0` row per fresh-pug type at a time. No division, no scheduled time — assumed to happen once enough people sign up. The creator gets `/manage`, `/edit`, `/connect-string`, and `/ping` scoped to that one fresh pug even if they're not a hoster. |

## Directory structure

```
pingu/
├── src/pingu/
│   ├── main.py
│   ├── config.py
│   ├── embeds.py
│   ├── scheduler.py
│   ├── cogs/
│   │   ├── hosting.py              # /host, /host-request wizard, /edit, /connect-string, /ping
│   │   ├── host_request.py         # on_message listener: mix-request thread roster capture
│   │   ├── manage.py               # /manage, /manage-signups
│   │   ├── linking.py              # /link-logs, /view-logs
│   │   ├── moderation.py           # flagged-content listener, /kill
│   │   └── tickets.py              # /ticket
│   ├── views/
│   │   ├── hosting_views.py        # host-request choice, fresh pug modal, mix-request review/edit
│   │   ├── roster_views.py         # CaptainReviewView, HosterPicksReviewView
│   │   ├── signup_views.py         # class buttons, sign-up flow, host-roster @mention check
│   │   ├── signout_views.py        # sign-out flow
│   │   ├── review_views.py         # hoster accept/deny review panels
│   │   ├── split_views.py          # oPUG team-splitting
│   │   ├── fresh_pug_manage_views.py
│   │   ├── roster_admin_views.py   # move-to-pending / restore-denied
│   │   └── manage_views.py         # main /manage panel (ManageView, SlimManageView)
│   ├── services/
│   │   ├── hosting_service.py
│   │   ├── fresh_pug_service.py
│   │   ├── roster_service.py
│   │   ├── match_lifecycle_service.py   # do_conclude/do_cancel, background archive+teardown
│   │   ├── log_service.py               # logs.tf lookup by roster SteamIDs
│   │   ├── moderation_service.py        # penalties: apply/expire
│   │   ├── channel_service.py
│   │   ├── ticket_export_service.py     # Excel mirror of tickets table
│   │   └── ticket_archive_service.py
│   ├── ui/
│   │   └── ui_updater.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── matches.py
│   │   ├── signups.py
│   │   ├── host_requests.py
│   │   ├── players.py
│   │   ├── match_logs.py
│   │   ├── penalties.py
│   │   └── tickets.py
│   └── templates/
│       ├── roster_instructions.py
│       ├── reminders.py
│       └── ticket_taxonomy.py
└── pingu_broadcast/
    └── main.py
```

`cogs/admin.py` and `db/guild_settings.py` were removed entirely — dead code from an early `/setup` design that was superseded by reading config straight from environment variables. There is no `roster_proposals` table; captain proposals live on `signups.captain_decision` instead (see design principle 5).

## cogs/ — Discord entry points

- **hosting.py** — `/host` (hoster-only wizard: mode → type → division → modal), `/edit`, `/connect-string`, `/ping`. Fresh pug's own creator can use `/edit`, `/connect-string`, and `/ping` on their own fresh pug even without the hoster role.
- **host_request.py** — listens for a requester's roster ping inside a mix-request thread, captures it, posts the formatted roster, pings the hoster role.
- **manage.py** — `/manage` (branches: pending roster-filled mix-request thread → accept/deny; fresh pug → conclude/cancel view; mix/oPUG → full `ManageView`) and `/manage-signups` (captain-only screening).
- **linking.py** — `/link-logs` (paste a logs.tf profile URL, grants `LOGS_LINKED_ROLE_ID`), `/view-logs` (open to everyone, look up anyone's linked profile).
- **moderation.py** — flagged-content `on_message` listener, and `/kill` (apply a Low Priority or Mix Ban penalty: player → type dropdown → one modal for number/unit/reason).
- **tickets.py** — `/ticket`: cascading dropdowns of variable depth (see `templates/ticket_taxonomy.py`) ending in a description modal.

## views/ — UI only

Discord `View`/`Modal` classes. Read state, call a service, done.

## services/ — business logic

No Discord API calls except through `ui_updater`.

- **roster_service.py** — `is_lp`, `reorder_class_roster`, `host_roster_user_ids` (parses `@mention`s out of a host roster — the only reliably-detectable entries), `captain_propose_accept`/`captain_propose_deny`, `finalise_accept`/`finalise_deny` (the one place a signup actually gets resolved, used by every accept/deny entry point), `commit_player_decisions`/`commit_all_captain_decisions` (Approve), `reject_player_decisions`/`reject_all_captain_decisions` (Reject). `finalise_accept` blocks accepting a Low-Priority player into a mix/oPUG until within 2 hours of kickoff; fresh pug is exempt (no kickoff time to measure against).
- **match_lifecycle_service.py** — `do_conclude`/`do_cancel` tear down channels and delete the ongoing-matches line immediately (no delayed notice). Archiving runs as a background task (`fire_archive_and_teardown`) so the hoster's own confirmation doesn't wait on it — posts progress in `HOSTER_CHANNEL_ID` at 25/50/75/100% of the thread-copy step specifically (the one part whose duration scales with thread size), sequenced strictly before teardown.
- **log_service.py** — searches logs.tf by the accepted roster's linked SteamID64s, scores candidates by roster overlap within a time window from kickoff, attaches qualifying logs to the archive summary. No API key needed (public logs.tf endpoints).
- **moderation_service.py** — `apply_penalty`/`expire_penalties`. `expire_penalties` looks up the correct role per-penalty by type (`low_prio` → `LOW_PRIO_ROLE_ID`, `mix_ban` → `MIX_BAN_ROLE_ID`), not a single shared role.
- **ticket_archive_service.py** — same archive-then-teardown background-task shape as `match_lifecycle_service`, retargeted: posts to `TICKET_ARCHIVE_CHANNEL_ID`, titles the archived thread with the ticket number.
- **ticket_export_service.py** — keeps a local `.xlsx` mirror of the tickets table (not the source of truth — SQLite is), updated incrementally. `openpyxl` is synchronous, so every call goes through `asyncio.to_thread()` to avoid blocking the event loop.

## db/ — one file per table, plain async functions

- **matches.py** — captain role is dynamic and per-team (`{team} Captain`, created/deleted with the match), not a static role. `channel_slot` is "how many of this team/division are currently active," not the match ID, so slot numbers get reused once a match concludes rather than climbing forever.
- **tickets.py** — `ticket_number` (`CAT-YYYYMMDD-NN`), counted per category per SGT calendar day, monotonic (a cancelled ticket's number is never reused, even within the same day).

## Tickets

`/ticket` → category dropdown → (if that branch has one) subcategory → type → one modal with a single description field. On submit: a fully private channel is created (reporter + `MOD_ROLE_ID` only — `@everyone` can't even view it), named after the ticket number. The reporter can see the channel but only type in its thread; mods can type in both. Resolve (mod-only) and Cancel (anyone with access) both go through a confirmation step, then archive-and-delete runs in the background.

## Fresh PUG

Divisionless, no scheduled time — created via a single modal (map only) once past the initial mode choice. `/ping` on a fresh pug just pings the generic PUG role.

## Config

Sourced entirely from environment variables (`.env`), read in `config.py`. No `/setup` command, no `guild_settings` table.

## pingu_broadcast/

Separate bot process and token. Reads the same SQLite file (read-only) to post approved promotions/mix announcements into other servers. Deliberately isolated from the layered structure above.