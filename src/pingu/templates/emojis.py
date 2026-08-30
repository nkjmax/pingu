"""
Every custom emoji ID used in match/ticket/ban messages, pulled out to
one place so they can be updated here if the server's emojis ever change,
without touching any message-building logic.
"""

# TF2 classes (Highlander)
CLASS_EMOJI = {
    "Scout":    "<:tf_scout:1536052419564150874>",
    "Soldier":  "<:tf_soldier:1536052610279153734>",
    "Pyro":     "<:tf_pyro:1174403417616175154>",
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

# Prepended to every match channel/VC's Discord name (text and voice
# alike, mix/opug/fresh pug alike) -- applied AFTER _slug() sanitizes the
# rest of the name, never fed through it, since _slug()'s regex would
# strip both the emoji and the separator right back out.
MATCH_CHANNEL_PREFIX = "\U0001f427\u30fb"  # "🐧・"