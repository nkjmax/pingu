import time
import logging
import discord

import pingu.db.penalties as penalties_db

log = logging.getLogger("moderation_service")

# Placeholder — swap for a real filter/classifier. Kept as a pure function
# so it's testable without touching Discord.
FLAGGED_TERMS: set[str] = set()


def contains_flagged_content(content: str) -> bool:
    lowered = content.lower()
    return any(term in lowered for term in FLAGGED_TERMS)


async def handle_violation(message: discord.Message, mod_log_channel: discord.abc.Messageable = None):
    try:
        await message.author.send(
            "Your message was flagged by the community's automated filter. "
            "Repeated violations may result in a penalty."
        )
    except discord.Forbidden:
        pass  # DMs closed — not fatal, just no direct warning delivered

    if mod_log_channel:
        await mod_log_channel.send(
            f"Flagged message from {message.author.mention} in {message.channel.mention}: "
            f"`{message.content[:200]}`"
        )


async def apply_penalty(guild: discord.Guild, user_id: int, penalty_type: str, issued_by: int,
                         reason: str = None, duration_seconds: int = None, role_id: int = None):
    expires_at = int(time.time()) + duration_seconds if duration_seconds else None
    await penalties_db.add_penalty(user_id, penalty_type, issued_by, reason, expires_at)

    if role_id:
        member = guild.get_member(user_id)
        role = guild.get_role(role_id)
        if member and role:
            await member.add_roles(role, reason=reason or f"Penalty: {penalty_type}")


async def expire_penalties(guild: discord.Guild, role_id: int = None):
    """Called by the scheduler sweep — removes the role for anything past expires_at."""
    expired = await penalties_db.get_expired_active_penalties()
    for penalty in expired:
        if role_id:
            member = guild.get_member(penalty["user_id"])
            role = guild.get_role(role_id)
            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Penalty expired")
                except discord.HTTPException:
                    pass
        await penalties_db.deactivate(penalty["id"])
