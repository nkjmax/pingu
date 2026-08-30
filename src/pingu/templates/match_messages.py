"""
Message templates for match creation posts -- mix, oPUG, fresh pug, and
their 6s variants. Plain .format()-able strings with placeholders; the
actual data gathering (roster lines, VC links, etc.) happens in embeds.py,
which pre-renders anything that's a loop (roster blocks) into a single
string block before filling these in, since a template placeholder can't
itself be a loop.

Edit wording here without touching embeds.py's data-gathering logic.
"""

FRESH_PUG_TEMPLATE = (
    "# {pinguu_icon} FRESH PUG{mode_suffix} {pug_ping}\n"
    "> **MAP**: {map_name}\n"
    "> **SERVER**: {server}\n"
    "> **HOSTER**: {hoster}\n"
    " \u2192 *Click {join_emoji} to join. **{cap} players** are needed to host.*\n"
    "{ping_icon}  **MATCH INFO**\n"
    "> - Fresh PUGs are open to players of **all divisions**\n"
    "> - Teams are re-picked before each map\n"
    "> - Captains **1v1 **as** Medic**. Winner picks first.\n"
    ">    -  Captains pick players from {fresh_lobby_vc}\n"
    ">   - The first map is **first come, first served**, so join early!\n"
    "{ping_icon}  **+1 PRIORITY**\n"
    "> - Playing Medic gives you **+1 priority** on the next map *(you can opt out)*\n"
    "> - Spectators can wait in {waiting_room_vc} to receive +1 priority\n"
    ">     - Joining {fresh_lobby_vc} before being picked may forfeit your +1 priority\n"
    "-#  - *Please review the {rules_channel} before signing up.*"
)

MIX_TEMPLATE = (
    "# {header_icon} {team_name} vs MIX TEAM\n"
    "> ## **DATE & TIME**: {date_time}\n"
    "> **DIVISION**: {division} \u00b7 {pug_ping}\n"
    "> **MAP**: {map_name}\n"
    "> **SERVER**: {server}\n"
    "> **HOSTER**: {hoster}\n"
    "{captain_line}"
    "{vc_line}"
    "**{team_name} Team**\n"
    "{host_roster_block}\n"
    "**MIX Team**\n"
    "{mix_roster_block}\n"
    "> **SUBS**: {subs}\n"
    "-#  {ping_icon} *Please review the {rules_channel} before signing up.*"
)

OPUG_TEMPLATE = (
    "# {header_icon} {header}\n"
    "> ## **DATE & TIME**: {date_time}\n"
    "> **DIVISION**: {division} \u00b7 {pug_ping}\n"
    "> **MAP**: {map_name}\n"
    "> **SERVER**: {server}\n"
    "> **HOSTER**: {hoster}\n"
    "{vc_lines}"
    "{roster_block}\n"
    "> **SUBS**: {subs}\n"
    "-#  {ping_icon} *Please review the {rules_channel} before signing up.*"
)