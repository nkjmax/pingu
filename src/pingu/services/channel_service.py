"""
Channel/VC structure per match type:

  mix        -> 1 text channel "mix-{team}-{slot}" + 1 VC, same name
  opug       -> 1 text channel "{div}-pug-{slot}" + 2 VCs "{div}-red-{slot}" / "{div}-blu-{slot}"
  fresh_pug  -> 1 text channel (post-only for hosters, everyone can react)
                + 4 VCs: waiting-room, fresh-lobby, fresh-red, fresh-blu
                (fresh pug is a singleton -- these exact names never collide,
                no slot needed)

`slot` is NOT the match_id -- it's "how many of this team/division are
CURRENTLY active", so a concluded/cancelled match's number gets reused by
the next one instead of climbing forever. Computed once at creation and
stored on the match row (channel_slot), so teardown reads the same value
back rather than recomputing it (recomputing at teardown time would give a
different, wrong answer once other matches have started/ended since).

Text channel permissions for mix/opug: only the hoster role (and, for
mix-request matches, the captain -- granted separately once that role
exists, see hosting_views.py) can send messages in the channel itself.
Everyone else can view, but only gets to type in the thread underneath it
-- denying send_messages on a channel does NOT restrict its threads, so
this is enough on its own without touching thread permissions.
"""

import re
import discord

from pingu import config
from pingu.db import matches as matches_db


def category_id_for_type(match_type: str):
    if match_type in ("6s_mix", "6s_opug", "6s_fresh_pug"):
        return config.SIXS_MATCH_CATEGORY_ID
    return config.MATCH_CATEGORY_ID


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")


def channel_label_for_match(match_type: str, slot: int, team_name: str = None, division: str = None) -> str:
    """Discord-safe (lowercase, hyphenated) text channel name for a match."""
    suffix = "-6s" if match_type.startswith("6s") else ""

    if match_type in ("mix", "6s_mix"):
        base = f"mix-{team_name or 'mix'}"
    elif match_type in ("opug", "6s_opug"):
        base = f"{division or 'pug'}-pug"
    elif match_type in ("fresh_pug", "6s_fresh_pug"):
        base = "fresh-pug"
    else:
        base = match_type

    slug = _slug(base + suffix)

    if match_type not in ("fresh_pug", "6s_fresh_pug"):
        slug = f"{slug}-{slot}"
    return slug[:100]


def _mix_opug_overwrites(guild: discord.Guild) -> dict:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
    }
    if config.HOSTER_ROLE_ID:
        hoster_role = guild.get_role(config.HOSTER_ROLE_ID)
        if hoster_role:
            overwrites[hoster_role] = discord.PermissionOverwrite(send_messages=True)
    return overwrites


async def create_match_channels(guild: discord.Guild, match_id: int, match_type: str,
                                  team_name: str = None, division: str = None, creator_id: int = None):
    """
    Creates the channels for a match. Returns (text_channel_id, category_id),
    or None if the category isn't configured/found.

    creator_id is only meaningful for fresh_pug/6s_fresh_pug -- unlike a
    hoster-created mix (whose creator is already a hoster by definition,
    gated at the /host command itself) or a mix-request captain (who gets
    channel access separately once their role exists), a fresh pug's
    creator might not be a hoster at all -- /host-request's fresh pug path
    is open to anyone. So they need explicit member-level send_messages,
    granted right at creation alongside the hoster-role overwrite.
    """
    category_id = category_id_for_type(match_type)
    if not category_id:
        return None

    category = guild.get_channel(category_id)
    if not category or not isinstance(category, discord.CategoryChannel):
        return None

    if match_type in ("mix", "6s_mix"):
        slot = await matches_db.count_active_by_key(match_type, "team_name", team_name)
    elif match_type in ("opug", "6s_opug"):
        slot = await matches_db.count_active_by_key(match_type, "division", division)
    else:
        slot = 1
    await matches_db.set_channel_slot(match_id, slot)

    label = channel_label_for_match(match_type, slot, team_name, division)

    if match_type in ("fresh_pug", "6s_fresh_pug"):
        text_channel = await _create_fresh_pug_channels(guild, category, label, creator_id=creator_id)
    elif match_type in ("mix", "6s_mix"):
        overwrites = _mix_opug_overwrites(guild)
        text_channel = await category.create_text_channel(name=label, overwrites=overwrites)
        await category.create_voice_channel(name=label)
    elif match_type in ("opug", "6s_opug"):
        overwrites = _mix_opug_overwrites(guild)
        text_channel = await category.create_text_channel(name=label, overwrites=overwrites)
        div_slug = _slug(division or "pug")
        await category.create_voice_channel(name=f"{div_slug}-red-{slot}")
        await category.create_voice_channel(name=f"{div_slug}-blu-{slot}")
    else:
        text_channel = await category.create_text_channel(name=label)

    await matches_db.set_category_id(match_id, category.id)
    return text_channel.id, category.id


async def _create_fresh_pug_channels(guild: discord.Guild, category: discord.CategoryChannel, label: str,
                                       creator_id: int = None):
    """
    Fresh pug's text channel is post-only for hosters (plus its own
    creator, see create_match_channels' docstring) -- everyone else can
    view and react (for the react-to-join flow) but not type. Adjust the
    hoster-role overwrite here if you don't use a single HOSTER_ROLE_ID.
    """
    everyone_overwrite = discord.PermissionOverwrite(
        view_channel=True, send_messages=False, add_reactions=True, read_message_history=True,
    )
    overwrites = {guild.default_role: everyone_overwrite}
    if config.HOSTER_ROLE_ID:
        hoster_role = guild.get_role(config.HOSTER_ROLE_ID)
        if hoster_role:
            overwrites[hoster_role] = discord.PermissionOverwrite(send_messages=True)
    if creator_id:
        creator = guild.get_member(creator_id)
        if creator:
            overwrites[creator] = discord.PermissionOverwrite(send_messages=True)

    text_channel = await category.create_text_channel(name=label, overwrites=overwrites)
    await category.create_voice_channel(name="waiting-room")
    await category.create_voice_channel(name="fresh-lobby")
    await category.create_voice_channel(name="fresh-red")
    await category.create_voice_channel(name="fresh-blu")
    return text_channel


async def grant_captain_channel_access(guild: discord.Guild, match_id: int, captain_role: discord.Role):
    """
    Called once the dynamic captain role exists (after channel creation,
    see hosting_views.py's accept flow) -- gives that role send_messages
    on the match's own text channel, same as the hoster role gets.
    """
    match = await matches_db.get_match(match_id)
    if not match or not match["channel_id"]:
        return
    channel = guild.get_channel(match["channel_id"])
    if channel:
        try:
            await channel.set_permissions(captain_role, send_messages=True)
        except discord.HTTPException:
            pass


async def teardown_match_channels(guild: discord.Guild, match_id: int):
    """
    Deletes this match's text channel and all voice channels alongside it
    in the same category with a matching name pattern -- never deletes the
    category itself, which is shared across all matches of that mode.

    Voice channel names are computed deterministically from the match's
    own stored channel_slot (not recomputed from current active-match
    counts, which would give a different, wrong answer here), and not read
    off a live text_channel object -- guild.get_channel() is a cache
    lookup, not an API call, and a miss there shouldn't mean deletion gets
    silently skipped.
    """
    match = await matches_db.get_match(match_id)
    if not match:
        return

    category_id = match["category_id"]
    category = guild.get_channel(category_id) if category_id else None
    if not category:
        try:
            category = await guild.fetch_channel(category_id) if category_id else None
        except discord.HTTPException:
            category = None

    match_type = match["type"]
    slot = match["channel_slot"] or 1

    text_channel = guild.get_channel(match["channel_id"]) if match["channel_id"] else None
    if not text_channel and match["channel_id"]:
        try:
            text_channel = await guild.fetch_channel(match["channel_id"])
        except discord.HTTPException:
            text_channel = None

    voice_names_to_delete = set()
    if match_type in ("mix", "6s_mix"):
        voice_names_to_delete.add(
            channel_label_for_match(match_type, slot, team_name=match["team_name"])
        )
    elif match_type in ("opug", "6s_opug"):
        div_slug = _slug(match["division"] or "pug")
        voice_names_to_delete.update({f"{div_slug}-red-{slot}", f"{div_slug}-blu-{slot}"})
    elif match_type in ("fresh_pug", "6s_fresh_pug"):
        voice_names_to_delete.update({"waiting-room", "fresh-lobby", "fresh-red", "fresh-blu"})

    if category and isinstance(category, discord.CategoryChannel):
        for vc in list(category.voice_channels):
            if vc.name in voice_names_to_delete:
                try:
                    await vc.delete(reason=f"Match #{match_id} archived")
                except discord.HTTPException:
                    pass

    if text_channel:
        try:
            await text_channel.delete(reason=f"Match #{match_id} archived")
        except discord.HTTPException:
            pass

    # Dynamic per-team captain role (mix-request matches only) -- deleted
    # alongside the channels, not left dangling on the server.
    if match["captain_role_id"]:
        role = guild.get_role(match["captain_role_id"])
        if role:
            try:
                await role.delete(reason=f"Match #{match_id} archived")
            except discord.HTTPException:
                pass