"""
Everything guild-specific comes from environment variables (a .env file
in practice). Original bot used a config.json + bot.config dict pattern
(config.get("key") everywhere) -- unified into this single module-attribute
style throughout the port so there's one config system, not two.

load_dotenv() is called explicitly here rather than relying on `uv run
--env-file .env` -- that flag has to be passed on every invocation, which
breaks under systemd or any launcher that isn't uv. Loading it in code
works the same way no matter how the process is started.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name):
    val = os.environ.get(name)
    return int(val) if val else None


def _env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN = os.environ.get("PINGU_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("PINGU_BOT_TOKEN environment variable is not set")

GUILD_ID = _env_int("PINGU_GUILD_ID")
ONGOING_CHANNEL_ID = _env_int("ONGOING_MATCHES_CHANNEL_ID")

HOSTER_ROLE_ID = _env_int("HOSTER_ROLE_ID")
LOW_PRIO_ROLE_ID = _env_int("LOW_PRIO_ROLE_ID")  # original bot's "LP" role -- roster sort order AND moderation penalty
LOGS_LINKED_ROLE_ID = _env_int("LOGS_LINKED_ROLE_ID")  # granted on successful /link-logs, separate from PUG_ROLE_ID
# PUG role: default @role pinged in match posts. No longer tied to
# verification -- see LOGS_LINKED_ROLE_ID above for that.
PUG_ROLE_ID = _env_int("PUG_ROLE_ID")

HOSTER_CHANNEL_ID = _env_int("HOSTER_CHANNEL_ID")  # original bot's 8h "please conclude" reminder channel
ARCHIVE_CHANNEL_ID = _env_int("ARCHIVE_CHANNEL_ID")
BALANCING_CHAT_ID = _env_int("BALANCING_CHAT_ID")  # oPUG team-split balancing chat
MOD_LOG_CHANNEL_ID = _env_int("MOD_LOG_CHANNEL_ID")
MIX_REQUESTS_CHANNEL_ID = _env_int("MIX_REQUESTS_CHANNEL_ID")

RE_SORT_ENABLED = _env_bool("RE_SORT_ENABLED", False)
RE_SORT_INTERVAL_MINUTES = int(os.environ.get("RE_SORT_INTERVAL_MINUTES", "30"))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # optional -- Pingu chatbot persona, silently disabled if unset

# Categories dynamic match channels get created under, split by game mode.
MATCH_CATEGORY_ID = _env_int("MATCH_CATEGORY_ID")        # HL: mix, opug, fresh_pug
SIXS_MATCH_CATEGORY_ID = _env_int("SIXS_MATCH_CATEGORY_ID")  # 6s: 6s_mix, 6s_opug, 6s_fresh_pug

# name -> role_id, used for /ping's auto-selection and manual dropdown.
# Original config.json had this as one nested dict; five flat env vars here.
PING_ROLES = {
    k: v for k, v in {
        "Iron": _env_int("PING_ROLE_IRON"),
        "Steel": _env_int("PING_ROLE_STEEL"),
        "Silver": _env_int("PING_ROLE_SILVER"),
        "Plat": _env_int("PING_ROLE_PLAT"),
        "PUG": _env_int("PING_ROLE_PUG"),
    }.items() if v is not None
}