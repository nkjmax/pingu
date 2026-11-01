"""
Every custom emoji ID used in match/ticket/ban messages, pulled out to
one place so they can be updated here if the server's emojis ever change,
without touching any message-building logic.

Event themes: the active theme is picked automatically from the current
month in SGT (see _detect_event). Each themed set only needs to list what
it actually changes -- anything a theme doesn't define falls back to the
default emoji, so adding e.g. Christmas later is just filling in a dict.

The theme is chosen ONCE when this module is imported, so the bot needs a
restart to roll over at a month boundary. To force a theme (testing, or
extending an event past its month), set EVENT_THEME in .env to
"halloween", "christmas" or "none".
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

# month -> theme name. Add a month here to start a new event.
EVENT_MONTHS = {
    10: "halloween",
    12: "christmas",
}


def _detect_event() -> str:
    override = os.environ.get("EVENT_THEME", "").strip().lower()
    if override:
        return override
    month = datetime.now(ZoneInfo("Asia/Singapore")).month
    return EVENT_MONTHS.get(month, "none")


EVENT = _detect_event()

# ── Default emojis ───────────────────────────────────────────────────────────

# TF2 classes (Highlander)
CLASS_EMOJI = {
    "Scout":    "<:tf_scout:1536052419564150874>",
    "Soldier":  "<:tf_soldier:1536052610279153734>",
    "Pyro":     "<:tf_pyro:1536052640088064091>",
    "Demoman":  "<:tf_demo:1536052673466470430>",
    "Heavy":    "<:tf_heavy:1536052704785342535>",
    "Engineer": "<:tf_engineer:1536052742131425370>",
    "Medic":    "<:tf_medic:1536052768161140757>",
    "Sniper":   "<:tf_sniper:1536052799077621821>",
    "Spy":      "<:tf_spy:1536052825879224460>",
}

# TF2 classes (6s)
SIXS_CLASS_EMOJI = {
    "PScout":   "<:p_scout:1541362791959629875>",
    "FScout":   "<:f_scout:1541362849492901949>",
    "PSoldier": "<:p_soldier:1541362706198827028>",
    "Roamer":   "<:r_soldier:1541362745511911424>",
    "Demoman":  "<:tf_demo:1536052673466470430>",
    "Medic":    "<:tf_medic:1536052768161140757>",
}

# Standalone icons used in message headers/sections
PINGUU_ICON      = "<:pinguu:1538932246675722240>"       # fresh pug header
PINGU_HAPPY_ICON = "<:pingu_happy:1535331544091070504>"  # mix/opug header
PING_ICON        = "<:ping:1541356316658892860>"         # section markers
FRESH_PUG_JOIN_EMOJI = "\U0001f427"                       # 🐧 -- fresh pug sign-up button + "click to join" text
ALL_CLASSES_EMOJI = "<:teamfortress:1546962285593890856>"  # the "All" sign-up button (mix/opug and Open For All alike)

# ── Event overrides ──────────────────────────────────────────────────────────
# Only list what changes. Keys in the class dicts must match the default
# dict's keys; any key left out keeps its default emoji.

_THEMES = {
    "halloween": {
        "CLASS_EMOJI": {
            "Scout":    "<:spooky_scout:1559555533428625449>",
            "Soldier":  "<:spooky_solly:1559555482400718848>",
            "Pyro":     "<:spooky_pyro:1559555435193704478>",
            "Demoman":  "<:spooky_demoman:1559555398741131395>",
            "Heavy":    "<:spooky_heavy:1559555204469493771>",
            "Engineer": "<:spooky_engineer:1559555365757255761>",
            "Medic":    "<:spooky_medic:1559555321637380176>",
            "Sniper":   "<:spooky_sniper:1559555287659192320>",
            "Spy":      "<:spooky_spy:1559555240624259153>",
        },
        "PINGU_HAPPY_ICON": "<:spooky_pingu:1555567715501084712>",
    },
    "christmas": {
        "CLASS_EMOJI": {
            "Scout":    "<:tf_scout:1536052419564150874>",
            "Soldier":  "<:tf_soldier:1536052610279153734>",
            "Pyro":     "<:tf_pyro:1536052640088064091>",
            "Demoman":  "<:tf_demo:1536052673466470430>",
            "Heavy":    "<:tf_heavy:1536052704785342535>",
            "Engineer": "<:tf_engineer:1536052742131425370>",
            "Medic":    "<:tf_medic:1536052768161140757>",
            "Sniper":   "<:tf_sniper:1536052799077621821>",
            "Spy":      "<:tf_spy:1536052825879224460>",
        },
        "PINGU_HAPPY_ICON": "<:pingu_happy:1535331544091070504>",
    },
}

_theme = _THEMES.get(EVENT, {})
CLASS_EMOJI = {**CLASS_EMOJI, **_theme.get("CLASS_EMOJI", {})}
SIXS_CLASS_EMOJI = {**SIXS_CLASS_EMOJI, **_theme.get("SIXS_CLASS_EMOJI", {})}
PINGUU_ICON = _theme.get("PINGUU_ICON", PINGUU_ICON)
PINGU_HAPPY_ICON = _theme.get("PINGU_HAPPY_ICON", PINGU_HAPPY_ICON)
PING_ICON = _theme.get("PING_ICON", PING_ICON)

# Prepended to every match channel/VC's Discord name (text and voice
# alike, mix/opug/fresh pug alike) -- applied AFTER _slug() sanitizes the
# rest of the name, never fed through it, since _slug()'s regex would
# strip both the emoji and the separator right back out.
MATCH_CHANNEL_PREFIX = "\U0001f427・"  # "🐧・"