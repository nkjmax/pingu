"""
Ticket archival on resolve/cancel -- copies the ticket's discussion
thread into a new thread under a summary message in
TICKET_ARCHIVE_CHANNEL_ID, titled with the ticket number, then deletes
the ticket's own channel. Runs as a background task so the confirming
mod/reporter's own response doesn't wait on it, and teardown is strictly
sequenced after archiving finishes so it can never race ahead and delete
the thread archival still needs to read from.

Deliberately no progress bar and no success confirmation message here --
unlike match archival, ticket threads are small/short-lived enough that
the extra visibility isn't worth the noise. A failure (all 3 attempts
exhausted) still gets posted, since a silently-lost ticket is the kind
of thing that's genuinely hard to notice later.
"""

import asyncio
import logging
import discord

log = logging.getLogger("ticket_archive_service")

from pingu import config


async def _archive_ticket_thread(client, ticket, archive_summary_msg):
    """Copies the ticket's discussion thread into a new thread under
    archive_summary_msg, titled with the ticket number."""
    if not ticket["thread_id"]:
        return

    thread = client.get_channel(ticket["thread_id"])
    if not thread:
        try:
            thread = await client.fetch_channel(ticket["thread_id"])
        except Exception:
            return

    messages = []
    try:
        async for msg in thread.history(limit=500, oldest_first=True):
            messages.append(msg)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch ticket thread history: {e}")

    if not messages:
        return

    archive_thread = await archive_summary_msg.create_thread(name=f"{ticket['ticket_number']} \u2014 thread log")

    for msg in messages:
        if msg.content or msg.embeds or msg.attachments:
            author = msg.author.display_name
            ts = discord.utils.format_dt(msg.created_at, style="t")
            content_lines = [f"**{author}** {ts}"]
            if msg.content:
                content_lines.append(msg.content)
            text = "\n".join(content_lines)
            while len(text) > 2000:
                await archive_thread.send(text[:2000])
                text = text[2000:]
            if text.strip():
                try:
                    await archive_thread.send(text)
                except Exception:
                    pass
            for embed in msg.embeds:
                try:
                    await archive_thread.send(embed=embed)
                except Exception:
                    pass


def fire_ticket_archive_and_teardown(client, ticket, status: str, closed_by: int):
    """
    Background task: archive the ticket's thread (with retries), THEN
    delete the ticket's channel -- sequenced within one task so teardown
    can't race ahead of archiving. Fired via asyncio.create_task() so the
    confirming mod/reporter's own response doesn't wait on any of this.
    """
    ticket_id = ticket["id"]
    ticket_number = ticket["ticket_number"]

    async def _run():
        archive_ch = client.get_channel(config.TICKET_ARCHIVE_CHANNEL_ID) if config.TICKET_ARCHIVE_CHANNEL_ID else None

        archived_ok = False
        if archive_ch:
            for attempt in range(1, 4):
                try:
                    body_lines = ticket["body"].split("\n")
                    blockquoted = "\n".join(f"> {line}" for line in body_lines)
                    summary_msg = await archive_ch.send(
                        f"\U0001f4e9 **Ticket #{ticket_number}**\n"
                        f"**Category:** {ticket['ticket_type']}\n"
                        f"**Submitted by:** <@{ticket['user_id']}>\n"
                        f"**Status:** {status}\n"
                        f"**Closed by:** <@{closed_by}>\n"
                        f"{blockquoted}\n"
                        f"** **"
                    )
                    await _archive_ticket_thread(client, ticket, summary_msg)
                    archived_ok = True
                    break
                except Exception as e:
                    log.warning(f"ticket archive attempt {attempt}/3 failed for ticket #{ticket_number}: {e}")
                    if attempt < 3:
                        await asyncio.sleep(3)

            if not archived_ok:
                try:
                    await archive_ch.send(f"\u274c Archiving failed for ticket #{ticket_number}. Please check logs.")
                except Exception:
                    pass
        else:
            log.warning(f"ticket archive skipped for ticket #{ticket_number}: TICKET_ARCHIVE_CHANNEL_ID not configured")

        channel = client.get_channel(ticket["channel_id"]) if ticket["channel_id"] else None
        if channel:
            try:
                await channel.delete(reason=f"Ticket #{ticket_number} {status} by {closed_by}")
            except discord.HTTPException:
                pass

    asyncio.create_task(_run())