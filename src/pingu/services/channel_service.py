"""
Creates a text channel + 2 voice channels for a match, as children of an
existing category (not a freshly-created one -- the category is yours,
provided via MATCH_CATEGORY_ID / SIXS_MATCH_CATEGORY_ID). Applies to every
match type now: mix, opug, fresh pug, and their 6s variants. Torn down on
archive.

Which category a match lands in is purely HL vs 6s -- see
category_id_for_type().
"""

import re
import discord

from pingu import config
from pingu.db import matches as matches_db


def category_id_for_type(match_type: str):
    if match_type in ("6s_mix", "6s_opug", "6s_fresh_pug"):
        return config.SIXS_MATCH_CATEGORY_ID
    return config.MATCH_CATEGORY_ID


def channel_label_for_match(match_type: str, match_id: int, team_name: str = None, division: str = None) -> str:
    """Discord-safe (lowercase, hyphenated) channel name for a match."""
    if match_type in ("mix", "6s_mix"):
        base = f"{team_name or 'mix'}-vs-mix"
    elif match_type in ("opug", "6s_opug"):
        base = f"{division or 'pug'}-pug"
    elif match_type in ("fresh_pug", "6s_fresh_pug"):
        base = "fresh-pug"
    else:
        base = match_type

    if match_type.startswith("6s"):
        base += "-6s"

    slug = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-")
    return f"{slug}-{match_id}"[:100]  # Discord channel name length cap


async def create_match_channels(guild: discord.Guild, match_id: int, match_type: str,
                                  team_name: str = None, division: str = None) -> int | None:
    """
    Creates the text channel + 2 VCs under the appropriate category, scoped
    to nothing special permission-wise (category's own permissions apply --
    set overwrites on the category itself if you want match channels
    private). Returns the category_id used, or None if that category isn't
    configured/found (channels are skipped in that case, not an error --
    caller can decide whether to warn the hoster).
    """
    category_id = category_id_for_type(match_type)
    if not category_id:
        return None

    category = guild.get_channel(category_id)
    if not category or not isinstance(category, discord.CategoryChannel):
        return None

    label = channel_label_for_match(match_type, match_id, team_name, division)

    text_channel = await category.create_text_channel(name=label)
    await category.create_voice_channel(name=f"RED ({match_id})")
    await category.create_voice_channel(name=f"BLU ({match_id})")

    await matches_db.set_category_id(match_id, category.id)
    return text_channel.id, category.id


async def teardown_match_channels(guild: discord.Guild, match_id: int):
    """
    Deletes only the channels this match created (its own text channel plus
    any voice channels it made) -- never deletes the category itself, since
    that's shared across all matches of that game mode.
    """
    match = await matches_db.get_match(match_id)
    if not match:
        return

    channel = guild.get_channel(match["channel_id"]) if match["channel_id"] else None
    if channel:
        try:
            await channel.delete(reason=f"Match #{match_id} archived")
        except discord.HTTPException:
            pass

    # Voice channels aren't tracked by ID on the match row -- found by name
    # match within the match's category instead. Names are unique per
    # match (include match_id), so this is unambiguous even with several
    # concurrent matches of the same type in the same category.
    category_id = match["category_id"]
    if category_id:
        category = guild.get_channel(category_id)
        if category and isinstance(category, discord.CategoryChannel):
            marker = f"({match_id})"
            for vc in list(category.voice_channels):
                if marker in vc.name:
                    try:
                        await vc.delete(reason=f"Match #{match_id} archived")
                    except discord.HTTPException:
                        pass
