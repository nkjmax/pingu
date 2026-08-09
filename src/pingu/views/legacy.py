"""
Ported faithfully from the original bot's views.py -- signup flows,
accept/deny review panels, sign-out with LP warnings, team splitting,
fresh pug management, and the archive orchestration (do_archive /
fire_archive_task with 3x retry). This is the piece everything else in
this project has been referring to as pingu.views.legacy.

The one deliberate change: archive_service.py (new, for the logs.tf
feature) now calls into do_archive here rather than views.py's version
being duplicated -- do_archive gained an optional matched_logs param
that flows through to build_archive_message, everything else unchanged.
"""

import asyncio
import time
import logging
import discord
from discord import ui

log = logging.getLogger("views.legacy")

from pingu.embeds import (
    TF2_CLASSES, CLASS_EMOJI, build_mix_message, build_match_embed, build_archive_message,
    build_opug_teams_message, build_split_view_text, build_pending_message, build_denied_message,
    SIXS_CLASSES, SIXS_CLASS_EMOJI, build_6s_opug_teams_message, build_6s_split_view_text,
    build_fresh_pug_signup_list,
)
from pingu import config
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services import channel_service


async def is_lp(client, user_id):
    """Check if a user has the LP (Low Priority) role."""
    if not config.LOW_PRIO_ROLE_ID or not config.GUILD_ID:
        return False
    try:
        guild  = client.get_guild(config.GUILD_ID)
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        return any(r.id == config.LOW_PRIO_ROLE_ID for r in member.roles)
    except Exception:
        return False


async def reorder_class_roster(client, match_id, class_name):
    """
    Enforce priority order for a class roster:
    1. Non-LP players sorted by accepted_at ASC
    2. LP players sorted by accepted_at ASC (always after all non-LP)
    """
    accepted = await signups_db.get_accepted_signups_for_class(match_id, class_name)
    if len(accepted) < 2:
        return

    lp_results = await asyncio.gather(*[is_lp(client, s["user_id"]) for s in accepted])
    lp_flags   = {s["id"]: result for s, result in zip(accepted, lp_results)}

    sorted_order = sorted(
        accepted,
        key=lambda s: (lp_flags[s["id"]], s["accepted_at"] if s["accepted_at"] is not None else float("inf"))
    )

    if [s["id"] for s in sorted_order] == [s["id"] for s in accepted]:
        return

    base    = min((s["accepted_at"] for s in accepted if s["accepted_at"] is not None), default=int(time.time()))
    updates = [(s["id"], base + i) for i, s in enumerate(sorted_order)]
    await signups_db.batch_set_accepted_at(updates)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def refresh_message(client, match_id):
    match   = await matches_db.get_match(match_id)
    signups = await signups_db.get_signups_for_match(match_id)
    if not match or not match["message_id"]:
        return
    try:
        channel = client.get_channel(match["channel_id"])
        if not channel:
            return
        msg = await channel.fetch_message(match["message_id"])
        pug_role_id = config.PUG_ROLE_ID
        if match["type"] == "mix":
            await msg.edit(content=build_mix_message(match, signups, pug_role_id=pug_role_id), embed=None)
        elif match["type"] == "opug":
            from pingu.embeds import build_opug_message
            await msg.edit(content=build_opug_message(match, signups, pug_role_id=pug_role_id), embed=None)
        elif match["type"] == "6s_mix":
            from pingu.embeds import build_6s_mix_message
            await msg.edit(content=build_6s_mix_message(match, signups, pug_role_id=pug_role_id), embed=None)
        elif match["type"] == "6s_opug":
            from pingu.embeds import build_6s_opug_message
            await msg.edit(content=build_6s_opug_message(match, signups, pug_role_id=pug_role_id), embed=None)
        else:
            await msg.edit(embed=build_match_embed(match, signups))
    except Exception as e:
        log.warning(f"refresh_message (main) failed for match #{match_id}: {e}")

    if match["type"] in ("mix", "6s_mix", "opug", "6s_opug"):
        channel = client.get_channel(match["channel_id"])
        if channel:
            pending_msg_id = match["pending_msg_id"]
            denied_msg_id = match["denied_msg_id"]

            if pending_msg_id:
                try:
                    pmsg = await channel.fetch_message(pending_msg_id)
                    await pmsg.edit(content=build_pending_message(match, signups))
                except Exception as e:
                    log.warning(f"refresh_message (pending) failed for match #{match_id}: {e}")
            if denied_msg_id:
                try:
                    dmsg = await channel.fetch_message(denied_msg_id)
                    await dmsg.edit(content=build_denied_message(match, signups))
                except Exception as e:
                    log.warning(f"refresh_message (denied) failed for match #{match_id}: {e}")

    try:
        from pingu.cogs.hosting import refresh_ongoing_line
        await refresh_ongoing_line(client, match_id)
    except Exception:
        pass


async def archive_thread_to_channel(client, match, archive_ch, archive_summary_msg):
    """
    Fetch all messages from the match thread and re-post them as a new
    thread on the archive summary message. Raises on critical failures so
    callers can handle retries correctly.
    """
    if not match["thread_id"]:
        return

    thread = client.get_channel(match["thread_id"])
    if not thread:
        try:
            thread = await client.fetch_channel(match["thread_id"])
        except Exception:
            return

    messages = []
    try:
        async for msg in thread.history(limit=500, oldest_first=True):
            messages.append(msg)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch thread history: {e}")

    if not messages:
        return

    match_type = match["type"] if match["type"] else "mix"
    if match_type in ("opug", "6s_opug"):
        thread_log_name = f"{match['division'] or 'PUG'} PUG \u2014 thread log"
    elif match_type == "6s_mix":
        thread_log_name = f"{match['team_name'] or 'Mix'} vs Mix 6s \u2014 thread log"
    elif match_type in ("fresh_pug", "6s_fresh_pug"):
        thread_log_name = "Fresh PUG \u2014 thread log"
    else:
        thread_log_name = f"{match['team_name'] or 'Mix'} vs Mix \u2014 thread log"

    try:
        archive_thread = await archive_summary_msg.create_thread(name=thread_log_name)
    except Exception as e:
        raise RuntimeError(f"Failed to create archive thread: {e}")

    for msg in messages:
        if not msg.content and not msg.embeds and not msg.attachments:
            continue
        author = msg.author.display_name
        ts     = discord.utils.format_dt(msg.created_at, style="t")
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


async def do_archive(client, match_id, concluded: bool, opug_split=None, matched_logs=None):
    """
    Shared archive logic for both conclude and cancel. Split into two
    phases so retries don't duplicate the summary message:
    - Phase 1: post summary (raises on failure)
    - Phase 2: post thread log and lock original (best effort, non-raising)

    matched_logs is new -- optional list from log_service, flows through
    to build_archive_message for the logs.tf score/damage/link section.
    """
    match   = await matches_db.get_match(match_id)
    signups = await signups_db.get_signups_for_match(match_id) if opug_split is None else opug_split

    if not config.ARCHIVE_CHANNEL_ID:
        raise RuntimeError("do_archive: no ARCHIVE_CHANNEL_ID in .env")

    archive_ch = client.get_channel(config.ARCHIVE_CHANNEL_ID)
    if not archive_ch:
        raise RuntimeError(f"do_archive: could not find archive channel {config.ARCHIVE_CHANNEL_ID}")

    status_line = "\U0001f3c1 Concluded" if concluded else "\u274c Cancelled"
    summary     = build_archive_message(match, signups, matched_logs=matched_logs)
    full_text   = f"{status_line}\n{summary}"

    archive_msg = await archive_ch.send(full_text)

    try:
        await archive_thread_to_channel(client, match, archive_ch, archive_msg)
    except Exception as e:
        log.warning(f"do_archive: thread log failed for match #{match_id}: {e}")

    if match["thread_id"]:
        try:
            thread = client.get_channel(match["thread_id"])
            if not thread:
                thread = await client.fetch_channel(match["thread_id"])
            if thread:
                await thread.edit(locked=True)
                await thread.edit(archived=True)
        except Exception as e:
            log.warning(f"do_archive: failed to lock/archive thread {match['thread_id']}: {e}")


async def _archive_task(client, match_id, concluded, opug_split, hoster_ch, status_msg, match_label, triggered_by, matched_logs=None):
    """Background coroutine that runs do_archive with retries."""
    ping = f"<@{triggered_by}> " if triggered_by else ""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            await do_archive(client, match_id, concluded=concluded, opug_split=opug_split, matched_logs=matched_logs)
            try:
                await status_msg.edit(content=f"\u2705 {ping}{match_label} thread archived.")
            except Exception:
                pass
            return
        except Exception as e:
            log.warning(f"_archive_task attempt {attempt}/{max_attempts} failed for match #{match_id}: {e}")
            if attempt < max_attempts:
                await asyncio.sleep(3)

    try:
        await status_msg.edit(content=f"\u274c {ping}Archiving failed for {match_label} (match #{match_id}). Please check logs.")
    except Exception:
        pass


def fire_archive_task(client, match_id, concluded, opug_split=None, hoster_channel_id=None, triggered_by=None, matched_logs=None):
    """
    Fire do_archive as a background task. Posts 'Archiving...' to hoster
    channel pinging the triggering hoster, and edits it to success/failure.
    """
    hoster_ch = client.get_channel(int(hoster_channel_id)) if hoster_channel_id else None

    async def _get_label():
        match = await matches_db.get_match(match_id)
        if not match:
            return f"match #{match_id}"
        t = match["type"]
        if t in ("opug", "6s_opug"):
            return f"{match['division'] or 'PUG'} PUG"
        elif t == "6s_mix":
            return f"{match['team_name'] or 'Mix'} vs Mix 6s"
        elif t in ("fresh_pug", "6s_fresh_pug"):
            return "Fresh PUG" if t == "fresh_pug" else "Fresh PUG 6v6"
        else:
            return f"{match['team_name'] or 'Mix'} vs Mix"

    async def _run():
        match_label = await _get_label()
        ping = f"<@{triggered_by}> " if triggered_by else ""
        status_msg = None
        if hoster_ch:
            try:
                status_msg = await hoster_ch.send(f"\U0001f504 {ping}Archiving {match_label} thread...")
            except Exception:
                pass
        if status_msg:
            await _archive_task(client, match_id, concluded, opug_split, hoster_ch, status_msg, match_label, triggered_by, matched_logs=matched_logs)
        else:
            for attempt in range(1, 4):
                try:
                    await do_archive(client, match_id, concluded=concluded, opug_split=opug_split, matched_logs=matched_logs)
                    return
                except Exception as e:
                    log.warning(f"fire_archive_task attempt {attempt}/3 failed for match #{match_id}: {e}")
                    if attempt < 3:
                        await asyncio.sleep(3)

    asyncio.create_task(_run())


async def do_cancel(client, match_id):
    match = await matches_db.get_match(match_id)
    if not match or match["ended"]:
        return False

    accepted = await signups_db.get_accepted_signups(match_id)
    pings    = " ".join(dict.fromkeys(f"<@{s['user_id']}>" for s in accepted))
    channel  = client.get_channel(match["channel_id"])

    if channel:
        try:
            await channel.purge(limit=200, check=lambda m: m.author.id == client.user.id)
        except Exception:
            pass

    ongoing_channel_id = getattr(client, "ongoing_channel", None)
    if ongoing_channel_id and match["ongoing_msg_id"]:
        try:
            oc   = client.get_channel(ongoing_channel_id)
            omsg = await oc.fetch_message(match["ongoing_msg_id"])
            await omsg.delete()
        except Exception:
            pass

    await matches_db.end_match(match_id)

    match_type = match["type"]
    if match_type in ("mix", "6s_mix"):
        mode_label = f"**{match['team_name'] or 'Mix'} vs Mix{' 6s' if match_type == '6s_mix' else ''}**"
    elif match_type in ("opug", "6s_opug"):
        mode_label = f"**{match['division'] or 'PUG'} PUG{' (6s)' if match_type == '6s_opug' else ''}**"
    elif match_type in ("fresh_pug", "6s_fresh_pug"):
        mode_label = f"**Fresh PUG{' 6s' if match_type == '6s_fresh_pug' else ''}**"
    else:
        mode_label = "**Match**"

    cancel_embed = discord.Embed(
        title="\u274c Match Cancelled",
        description=(
            f"{mode_label}\n"
            f"Hosted by {match['created_by_name']} has been **cancelled**."
        ),
        colour=discord.Colour.red(),
    )
    cancel_embed.set_footer(text="This notice will be removed in 24 hours.")

    if channel:
        notice = await channel.send(
            content=f"\U0001f6a8 {pings}" if pings else None,
            embed=cancel_embed,
        )
        await matches_db.cancel_match(match_id, notice.id)

    fire_archive_task(client, match_id, concluded=False,
                      hoster_channel_id=config.HOSTER_CHANNEL_ID,
                      triggered_by=match["created_by"])

    return True


# ── Sign-out confirmation (last-hour warning) ─────────────────────────────────

class SignOutConfirmView(ui.View):
    def __init__(self, match_id, user_id, class_name=None):
        super().__init__(timeout=60)
        self.match_id   = match_id
        self.user_id    = user_id
        self.class_name = class_name

    @ui.button(label="Yes, sign out anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await do_signout(interaction.client, self.match_id, self.user_id, self.class_name)
        await interaction.edit_original_response(
            content="You have been signed out. Please find a replacement as soon as possible.",
            view=None,
        )

    @ui.button(label="Stay signed up", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="Sign-out cancelled. You're still signed up.", view=None
        )


class SignOutAllConfirmView(ui.View):
    def __init__(self, match_id, user_id):
        super().__init__(timeout=60)
        self.match_id = match_id
        self.user_id  = user_id

    @ui.button(label="Yes, sign out of everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        signups = await signups_db.get_non_denied_signups_for_user(self.match_id, self.user_id)
        for s in signups:
            await do_signout(interaction.client, self.match_id, self.user_id, s["class_name"])
        await interaction.edit_original_response(
            content="You have been signed out of all classes. Please find replacements as soon as possible.",
            view=None,
        )

    @ui.button(label="Stay signed up", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="Sign-out cancelled. You're still signed up.", view=None
        )


class SignOutClassPickerView(ui.View):
    """Shown when a user is signed up on multiple classes and wants to sign out."""
    def __init__(self, match_id, user_id, signups, within_hour, rostered_classes):
        super().__init__(timeout=60)
        self.match_id        = match_id
        self.user_id         = user_id
        self.within_hour     = within_hour
        self.rostered_classes = rostered_classes
        all_emojis = {**CLASS_EMOJI, **SIXS_CLASS_EMOJI}
        options = [
            discord.SelectOption(
                label=s["class_name"],
                value=s["class_name"],
                emoji=all_emojis.get(s["class_name"]),
                description=f"Status: {s['status']}"
            )
            for s in signups
        ]
        options.append(discord.SelectOption(
            label="All classes",
            value="_all",
            emoji="\U0001f6aa",
            description="Sign out of everything"
        ))
        select = ui.Select(placeholder="Select class to sign out of\u2026", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        value = interaction.data["values"][0]

        if value == "_all":
            if self.within_hour and self.rostered_classes:
                lp_warning = ""
                if config.LOW_PRIO_ROLE_ID:
                    lp_warning = f"\n\u26a0\ufe0f You will receive the <@&{config.LOW_PRIO_ROLE_ID}> role if you fail to find replacements."
                view = SignOutAllConfirmView(self.match_id, self.user_id)
                await interaction.response.edit_message(
                    content=f"\u26a0\ufe0f **Warning:** The match starts in less than 2 hours.{lp_warning}\nSign out of **all classes**?",
                    view=view,
                )
            else:
                await interaction.response.defer(ephemeral=True)
                signups = await signups_db.get_non_denied_signups_for_user(self.match_id, self.user_id)
                for s in signups:
                    await do_signout(interaction.client, self.match_id, self.user_id, s["class_name"])
                await interaction.followup.send("You have been signed out of all classes.", ephemeral=True)
            return

        class_name = value
        if self.within_hour and class_name in self.rostered_classes:
            lp_warning = ""
            if config.LOW_PRIO_ROLE_ID:
                lp_warning = f"\n\u26a0\ufe0f You will receive the <@&{config.LOW_PRIO_ROLE_ID}> role if you fail to find a replacement."
            view = SignOutConfirmView(self.match_id, self.user_id, class_name)
            await interaction.response.edit_message(
                content=f"\u26a0\ufe0f **Warning:** The match starts in less than 2 hours.{lp_warning}\nSign out of **{class_name}**?",
                view=view,
            )
        else:
            await interaction.response.defer(ephemeral=True)
            await do_signout(interaction.client, self.match_id, self.user_id, class_name)
            await interaction.followup.send(f"You have been signed out of **{class_name}**.", ephemeral=True)


async def do_signout(client, match_id, user_id, class_name=None):
    if class_name:
        signup = await signups_db.get_signup_by_user_and_class(match_id, user_id, class_name)
    else:
        signup = await signups_db.get_signup_by_user(match_id, user_id)
    if not signup:
        return None

    class_name   = signup["class_name"]
    was_accepted = signup["status"] == "accepted"
    match        = await matches_db.get_match(match_id)
    match_type   = match["type"] if match else "mix"
    is_opug      = match_type in ("opug", "6s_opug")

    accepted_for_class = await signups_db.get_accepted_signups_for_class(match_id, class_name)
    if is_opug:
        main_uids  = [s["user_id"] for s in accepted_for_class[:2]]
        is_rostered = was_accepted and user_id in main_uids
    else:
        is_rostered = was_accepted and len(accepted_for_class) > 0 and accepted_for_class[0]["user_id"] == user_id

    await signups_db.remove_signup(match_id, user_id, class_name)

    channel      = client.get_channel(match["channel_id"]) if match else None
    channel_name = channel.name if channel else "the match channel"

    if match_type in ("opug", "6s_opug"):
        match_label = f"{match['division'] or 'PUG'} PUG"
    elif match_type == "6s_mix":
        match_label = f"{match['team_name'] or 'Mix'} vs Mix 6s"
    else:
        match_label = f"{match['team_name'] or 'Mix'} vs Mix"

    if is_rostered:
        if is_opug:
            remaining = await signups_db.get_accepted_signups_for_class(match_id, class_name)
            if len(remaining) >= 2:
                newly_main = remaining[1]
                if match["thread_id"]:
                    try:
                        thread = client.get_channel(match["thread_id"])
                        if thread:
                            await thread.send(
                                f"<@{newly_main['user_id']}> you've been moved to the "
                                f"**{class_name}** main roster slot \u2014 the previous player signed out. \u2705"
                            )
                    except Exception:
                        pass
        else:
            next_sub = await signups_db.get_next_accepted_for_class(match_id, class_name, user_id)
            if next_sub:
                await signups_db.remove_sub_slots_for_user(match_id, next_sub["user_id"], class_name)
                if match["thread_id"]:
                    try:
                        thread = client.get_channel(match["thread_id"])
                        if thread:
                            await thread.send(
                                f"<@{next_sub['user_id']}> you've been moved to the "
                                f"**{class_name}** slot on the Mix Team \u2014 the previous player signed out. \u2705"
                            )
                    except Exception:
                        pass

                if config.HOSTER_CHANNEL_ID:
                    hoster_ch = client.get_channel(config.HOSTER_CHANNEL_ID)
                    if hoster_ch:
                        await hoster_ch.send(
                            f"<@{match['created_by']}> \u26a0\ufe0f **{signup['username']}** has signed out of "
                            f"**{class_name}** in <#{match['channel_id']}> ({match_label}). "
                            f"**{next_sub['username']}** has been moved to the main roster."
                        )
            else:
                if config.HOSTER_CHANNEL_ID:
                    hoster_ch = client.get_channel(config.HOSTER_CHANNEL_ID)
                    if hoster_ch:
                        await hoster_ch.send(
                            f"<@{match['created_by']}> \u26a0\ufe0f **{signup['username']}** has signed out of "
                            f"**{class_name}** in <#{match['channel_id']}> ({match_label})."
                        )

    elif was_accepted:
        pass

    await asyncio.sleep(0.5)
    await refresh_message(client, match_id)
    return signup


# ── Sign-out button ───────────────────────────────────────────────────────────

class SignOutButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(
            label="Sign Out",
            emoji="\U0001f6aa",
            custom_id=f"signout:{match_id}",
            style=discord.ButtonStyle.danger,
            row=4,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This match has already ended or been cancelled.", ephemeral=True
            )
            return

        signups = await signups_db.get_non_denied_signups_for_user(self.match_id, interaction.user.id)
        if not signups:
            await interaction.followup.send(
                "You're not signed up for this match.", ephemeral=True
            )
            return

        time_until = match["timestamp"] - time.time()
        within_hour = 0 < time_until <= 7200

        rostered_classes = set()
        if within_hour:
            for s in signups:
                if s["status"] == "accepted":
                    accepted_for_class = await signups_db.get_accepted_signups_for_class(self.match_id, s["class_name"])
                    if accepted_for_class and accepted_for_class[0]["user_id"] == interaction.user.id:
                        rostered_classes.add(s["class_name"])

        if len(signups) == 1:
            class_name = signups[0]["class_name"]
            if within_hour and class_name in rostered_classes:
                lp_warning = ""
                if config.LOW_PRIO_ROLE_ID:
                    lp_warning = f"\n\u26a0\ufe0f You will receive the <@&{config.LOW_PRIO_ROLE_ID}> role if you fail to find a replacement."
                view = SignOutConfirmView(self.match_id, interaction.user.id, class_name)
                await interaction.followup.send(
                    f"\u26a0\ufe0f **Warning:** The match starts in less than 2 hours.{lp_warning}\nAre you sure?",
                    view=view, ephemeral=True,
                )
            else:
                await do_signout(interaction.client, self.match_id, interaction.user.id, class_name)
                await interaction.followup.send("You have been signed out.", ephemeral=True)
        else:
            view = SignOutClassPickerView(self.match_id, interaction.user.id, signups, within_hour, rostered_classes)
            classes = ", ".join(f"**{s['class_name']}**" for s in signups)
            await interaction.followup.send(
                f"You're signed up on {classes}. Which class do you want to sign out of?",
                view=view, ephemeral=True,
            )


# ── Per-player decision view ──────────────────────────────────────────────────

class PlayerDecisionView(ui.View):
    def __init__(self, match_id, signup_id, username, class_name, channel_name):
        super().__init__(timeout=300)
        self.match_id     = match_id
        self.signup_id    = signup_id
        self.username     = username
        self.class_name   = class_name
        self.channel_name = channel_name

    @ui.button(label="\u2705 Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction, button):
        current = await signups_db.get_signup_by_id(self.signup_id)
        already = current and current["status"] == "accepted"
        filled  = await signups_db.count_accepted_for_class(self.match_id, self.class_name)

        await signups_db.update_signup_status(self.signup_id, "accepted")
        label = "added to subs list" if (filled >= 1 and not already) else f"accepted as **{self.class_name}**"

        await interaction.response.edit_message(
            content=f"\u2705 **{self.username}** {label} in **#{self.channel_name}**.",
            view=None,
        )
        await refresh_message(interaction.client, self.match_id)

    @ui.button(label="\u274c Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        await signups_db.update_signup_status(self.signup_id, "denied")
        await interaction.response.edit_message(
            content=f"\u274c **{self.username}** denied for **{self.class_name}** in **#{self.channel_name}**.",
            view=None,
        )
        await refresh_message(interaction.client, self.match_id)


# ── Sign-up buttons ───────────────────────────────────────────────────────────

class ClassButton(ui.Button):
    def __init__(self, class_name, match_id):
        super().__init__(
            label=class_name,
            emoji=CLASS_EMOJI[class_name],
            custom_id=f"signup:{match_id}:{class_name}",
            style=discord.ButtonStyle.secondary,
            row=TF2_CLASSES.index(class_name) // 5,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This match has already ended or been cancelled.", ephemeral=True
            )
            return

        all_signups = await signups_db.get_non_denied_signups_for_user(self.match_id, interaction.user.id)
        for s in all_signups:
            if s["status"] == "accepted":
                accepted_for = await signups_db.get_accepted_signups_for_class(self.match_id, s["class_name"])
                if accepted_for and accepted_for[0]["user_id"] == interaction.user.id:
                    await interaction.followup.send(
                        f"You're already on the main roster as **{s['class_name']}**. "
                        "Sign out first if you want to change classes.",
                        ephemeral=True,
                    )
                    return

        existing_class = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, self.class_name)
        if existing_class and existing_class["status"] == "cancelled":
            existing_class = None
        if existing_class:
            if existing_class["status"] == "denied":
                await interaction.followup.send(
                    f"You've been denied for **{self.class_name}**. Please contact the hoster.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"You're already signed up for **{self.class_name}**. "
                "Sign out of this class first if you want to change it.",
                ephemeral=True,
            )
            return

        clashing = await signups_db.get_accepted_matches_for_user(
            interaction.user.id,
            exclude_match_id=self.match_id,
            reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(
                f"{m['team_name'] or 'a mix'} (<#{m['channel_id']}>)" for m in clashing
            )
            view = ClashConfirmView(self.match_id, self.class_name, clash_names)
            warn = "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names + ". Are you sure you want to sign up for this mix too?"
            await interaction.followup.send(warn, view=view, ephemeral=True)
            return

        await _do_signup(interaction, self.match_id, self.class_name)


# ── Clash confirmation ────────────────────────────────────────────────────────

class ClashConfirmView(ui.View):
    def __init__(self, match_id, class_name, clash_names):
        super().__init__(timeout=60)
        self.match_id   = match_id
        self.class_name = class_name
        self.clash_names = clash_names

    @ui.button(label="Yes, sign up anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await _do_signup(interaction, self.match_id, self.class_name)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="Sign-up cancelled.", view=None
        )


async def _do_signup(interaction, match_id, class_name):
    """Shared signup logic used by ClassButton and ClashConfirmView."""
    match = await matches_db.get_match(match_id)

    signup_id = await signups_db.add_signup(
        match_id, interaction.user.id,
        interaction.user.display_name, class_name,
    )
    if signup_id is None:
        await interaction.followup.send("Could not add sign-up. Try again.", ephemeral=True)
        return

    await interaction.followup.send(
        f"\u2705 Signed up as **{class_name}**! The host will review shortly.",
        ephemeral=True,
    )

    match_row = await matches_db.get_match(match_id)

    clashing = await signups_db.get_accepted_matches_for_user(
        interaction.user.id,
        exclude_match_id=match_id,
        reference_timestamp=match["timestamp"]
    )
    if clashing and config.HOSTER_CHANNEL_ID:
        hoster_ch = interaction.client.get_channel(config.HOSTER_CHANNEL_ID)
        if hoster_ch:
            match_type = match_row["type"]
            if match_type in ("opug", "6s_opug"):
                this_match_label = f"{match_row['division'] or 'PUG'} PUG"
            elif match_type == "6s_mix":
                this_match_label = f"{match_row['team_name'] or 'Mix'} vs Mix 6s"
            else:
                this_match_label = f"{match_row['team_name'] or 'Mix'} vs Mix"

            hoster_pings = {match_row["created_by"]}
            for m in clashing:
                hoster_pings.add(m["created_by"])
            pings_str = " ".join(f"<@{uid}>" for uid in hoster_pings)

            def clash_label(m):
                t = m["type"] if m["type"] else "mix"
                if t in ("opug", "6s_opug"):
                    return f"<#{m['channel_id']}> ({m['division'] or 'PUG'} PUG)"
                elif t == "6s_mix":
                    return f"<#{m['channel_id']}> ({m['team_name'] or 'Mix'} vs Mix 6s)"
                else:
                    return f"<#{m['channel_id']}> ({m['team_name'] or 'Mix'} vs Mix)"

            clash_refs = ", ".join(clash_label(m) for m in clashing)
            await hoster_ch.send(
                f"{pings_str} \u26a0\ufe0f **{interaction.user.display_name}** signed up for **{class_name}** "
                f"in <#{match_row['channel_id']}> ({this_match_label}) "
                f"but is already accepted in {clash_refs}."
            )

    await refresh_message(interaction.client, match_id)


# ── Organised PUG sign-up view ────────────────────────────────────────────────

class OPugClassButton(ui.Button):
    def __init__(self, class_name, match_id):
        super().__init__(
            label=class_name,
            emoji=CLASS_EMOJI[class_name],
            custom_id=f"opug_signup:{match_id}:{class_name}",
            style=discord.ButtonStyle.secondary,
            row=TF2_CLASSES.index(class_name) // 5,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This PUG has already ended or been cancelled.", ephemeral=True
            )
            return

        existing_class = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, self.class_name)
        if existing_class and existing_class["status"] == "cancelled":
            existing_class = None
        if existing_class:
            if existing_class["status"] == "denied":
                await interaction.followup.send(
                    f"You've been denied for **{self.class_name}**. Please contact the hoster.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"You're already signed up for **{self.class_name}**.", ephemeral=True
            )
            return

        all_signups = await signups_db.get_non_denied_signups_for_user(self.match_id, interaction.user.id)
        for s in all_signups:
            if s["status"] == "accepted":
                accepted_for = await signups_db.get_accepted_signups_for_class(self.match_id, s["class_name"])
                main_uids = [a["user_id"] for a in accepted_for[:2]]
                if interaction.user.id in main_uids:
                    await interaction.followup.send(
                        f"You're already on the main roster as **{s['class_name']}**. "
                        "Sign out first if you want to change classes.",
                        ephemeral=True,
                    )
                    return
        clashing = await signups_db.get_accepted_matches_for_user(
            interaction.user.id,
            exclude_match_id=self.match_id,
            reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(
                f"{m['team_name'] or 'a mix'} (<#{m['channel_id']}>)" for m in clashing
            )
            view = ClashConfirmView(self.match_id, self.class_name, clash_names)
            warn = "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names + ". Are you sure you want to sign up for this PUG too?"
            await interaction.followup.send(warn, view=view, ephemeral=True)
            return

        await _do_signup(interaction, self.match_id, self.class_name)


class OPugSignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in TF2_CLASSES:
            self.add_item(OPugClassButton(cls, match_id))
        self.add_item(SignOutButton(match_id))


class SignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in TF2_CLASSES:
            self.add_item(ClassButton(cls, match_id))
        self.add_item(SignOutButton(match_id))


# ── Withdraw ──────────────────────────────────────────────────────────────────

class WithdrawView(ui.View):
    def __init__(self, match_id, user_id):
        super().__init__(timeout=60)
        self.match_id = match_id
        self.user_id  = user_id

    @ui.button(label="Withdraw sign-up", style=discord.ButtonStyle.danger)
    async def withdraw(self, interaction, button):
        await do_signout(interaction.client, self.match_id, self.user_id)
        await interaction.response.edit_message(content="Your sign-up has been withdrawn.", view=None)

    @ui.button(label="Keep sign-up", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, button):
        await interaction.response.edit_message(content="No changes made.", view=None)


# ── Conclude confirmation ─────────────────────────────────────────────────────

class ConcludeConfirmView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, conclude match", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)
        if not match:
            await interaction.followup.send("Match not found.", ephemeral=True)
            return

        channel = interaction.client.get_channel(match["channel_id"])

        if channel:
            try:
                await channel.purge(limit=200, check=lambda m: m.author.id == interaction.client.user.id)
            except Exception:
                pass

        ongoing_channel_id = getattr(interaction.client, "ongoing_channel", None)
        if ongoing_channel_id and match["ongoing_msg_id"]:
            try:
                oc   = interaction.client.get_channel(ongoing_channel_id)
                omsg = await oc.fetch_message(match["ongoing_msg_id"])
                await omsg.delete()
            except Exception:
                pass

        if channel:
            accepted = await signups_db.get_accepted_signups(self.match_id)
            seen, pings = set(), []
            for s in accepted:
                if s["user_id"] not in seen:
                    seen.add(s["user_id"])
                    pings.append(f"<@{s['user_id']}>")
            ping_str = " ".join(pings)
            if match["type"] in ("opug", "6s_opug"):
                division = match["division"] or "PUG"
                notice_text = f"\U0001f3c1 **{division} PUG** has been concluded. Thanks for playing! \U0001fae1"
            else:
                team = match["team_name"] or "Mix"
                notice_text = f"{ping_str}\n\U0001f3c1 **{team} vs Mix Team** has been concluded. Thanks for playing! \U0001fae1"
            conclude_msg = await channel.send(notice_text)
            await matches_db.set_conclude_msg(self.match_id, conclude_msg.id, match["channel_id"])

        await matches_db.end_match(self.match_id)

        opug_split = None
        if match["type"] in ("opug", "6s_opug"):
            split = await matches_db.get_team_split(self.match_id)
            if split:
                all_signups  = await signups_db.get_signups_for_match(self.match_id)
                accepted_all = [s for s in all_signups if s["status"] == "accepted"]
                red_uids     = set(split["red"])
                blu_uids     = set(split["blu"])
                opug_split   = {
                    "red":  [s for s in accepted_all if s["user_id"] in red_uids],
                    "blu":  [s for s in accepted_all if s["user_id"] in blu_uids],
                    "subs": [s for s in accepted_all if s["user_id"] not in red_uids and s["user_id"] not in blu_uids],
                }

        # Best-effort logs.tf lookup -- never blocks archiving on failure.
        matched_logs = None
        try:
            from pingu.services import log_service
            matched_logs = await log_service.find_and_attach_logs(match)
        except Exception as e:
            log.warning(f"logs.tf lookup failed for match #{self.match_id}: {e}")

        fire_archive_task(interaction.client, self.match_id, concluded=True,
                          opug_split=opug_split, hoster_channel_id=config.HOSTER_CHANNEL_ID,
                          triggered_by=interaction.user.id, matched_logs=matched_logs)
        await interaction.followup.send("\u2705 Match concluded. Archiving in background...", ephemeral=True)

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Conclusion cancelled.", view=None)


# ── Cancel confirmation ───────────────────────────────────────────────────────

class CancelConfirmView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, cancel the match", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        success = await do_cancel(interaction.client, self.match_id)
        if success:
            await interaction.followup.send(
                "\u2705 Match cancelled. Notice posted for 24 hours.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "\u274c Could not cancel \u2014 match may already be ended.", ephemeral=True
            )

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(
            content="Cancellation aborted. Match is still active.", view=None
        )


# ── Manage overview helpers ──────────────────────────────────────────────────

async def build_manage_text(match_id):
    signups = await signups_db.get_signups_for_match(match_id)
    match   = await matches_db.get_match(match_id)
    team    = match["team_name"] or "Mix"

    player_data = {}
    for s in signups:
        uid = s["user_id"]
        if uid not in player_data:
            player_data[uid] = {"username": s["username"], "accepted": [], "pending": [], "denied": [], "min_id": s["id"]}
        player_data[uid][s["status"]].append((s["id"], s["class_name"]))

    class_list = SIXS_CLASSES if match["type"] in ("6s_mix", "6s_opug", "6s_fresh_pug") else TF2_CLASSES

    for uid in player_data:
        for key in ("accepted", "pending", "denied"):
            player_data[uid][key].sort(key=lambda x: x[0])
            player_data[uid][key] = [cls for _, cls in player_data[uid][key]]

    def fmt(classes):
        return ", ".join(classes) if classes else "\u2014"

    if match["type"] in ("opug", "6s_opug"):
        division = match["division"] or "PUG"
        header = "**" + division + " PUG \u2014 signups**\n"
    elif match["type"] in ("fresh_pug", "6s_fresh_pug"):
        header = "**Fresh PUG \u2014 signups**\n"
    elif match["type"] == "6s_mix":
        header = "**" + team + " vs Mix 6s \u2014 signups**\n"
    else:
        header = "**" + team + " vs Mix \u2014 signups**\n"
    lines  = [header]

    accepted_players = [p for p in player_data.values() if p["accepted"]]
    pending_players  = sorted(
        [p for p in player_data.values() if p["pending"]],
        key=lambda p: p["min_id"]
    )
    denied_players   = [p for p in player_data.values() if p["denied"] and not p["accepted"] and not p["pending"]]

    if accepted_players:
        lines.append("\u2705 **Accepted:**")
        for p in accepted_players:
            lines.append("\u2022 **" + p["username"] + "** \u2014 " + fmt(p["accepted"]))

    if pending_players:
        lines.append("\n\u23f3 **Pending** *(chronological order)*:")
        for p in pending_players:
            lines.append("\u2022 **" + p["username"] + "** \u2014 " + fmt(p["pending"]))

    if denied_players:
        lines.append("\n\u274c **Denied:**")
        for p in denied_players:
            lines.append("\u2022 **" + p["username"] + "** \u2014 " + fmt(p["denied"]))

    if not accepted_players and not pending_players and not denied_players:
        lines.append("No sign-ups yet.")

    total_pending = sum(len(p["pending"]) for p in player_data.values())
    return "\n".join(lines), total_pending


class ClassDropdownSelect(ui.Select):
    """Dropdown to pick a class -- only shows classes with pending signups."""
    def __init__(self, match_id, pending_by_class, is_sixs=False):
        self.match_id = match_id
        self.is_sixs  = is_sixs
        class_list    = SIXS_CLASSES if is_sixs else TF2_CLASSES
        options = []
        for cls in class_list:
            count = len(pending_by_class.get(cls, []))
            if count:
                options.append(discord.SelectOption(
                    label=cls,
                    value=cls,
                    description=str(count) + " pending",
                ))
        if not options:
            options = [discord.SelectOption(label="No pending", value="_none", description="No pending sign-ups")]
        super().__init__(
            placeholder="Select a class to review...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction):
        class_name = self.values[0]
        pending    = await signups_db.get_pending_signups(self.match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == class_name], key=lambda s: s["id"])

        if not class_pend:
            await interaction.response.edit_message(
                content="No pending sign-ups for **" + class_name + "** anymore.",
                view=await ReviewView.create(self.match_id),
            )
            return

        view = PlayerPickView(self.match_id, class_name, class_pend)
        text = "**" + class_name + "**  \u2014  click a player to accept *(chronological order)*"
        await interaction.response.edit_message(content=text, view=view)


class LPConfirmView(ui.View):
    def __init__(self, match_id, signup_id, username, class_name, filled):
        super().__init__(timeout=60)
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name
        self.filled     = filled

    @ui.button(label="Yes, accept anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        current = await signups_db.get_signup_by_id(self.signup_id)
        already = current and current["status"] == "accepted"
        if already:
            await interaction.followup.send(
                f"\u274c **{self.username}** has already been accepted.", ephemeral=True
            )
            return
        await _do_accept(interaction, self.match_id, self.signup_id, self.username, self.class_name, self.filled, already, current)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


async def _do_accept(interaction, match_id, signup_id, username, class_name, filled, already, current):
    await signups_db.update_signup_status(signup_id, "accepted")

    match = await matches_db.get_match(match_id)
    is_opug = match and match["type"] in ("opug", "6s_opug")

    if is_opug:
        is_main_roster = filled < 2 and not already
    else:
        is_main_roster = filled == 0 and not already

    user_id = current["user_id"] if current else None

    if is_main_roster and user_id:
        await signups_db.remove_sub_slots_for_user(match_id, user_id, class_name)
        await signups_db.remove_pending_slots_for_user(match_id, user_id, class_name)

    await reorder_class_roster(interaction.client, match_id, class_name)
    await refresh_message(interaction.client, match_id)

    accepted_after = await signups_db.get_accepted_signups_for_class(match_id, class_name)
    if is_opug:
        main_uids = [s["user_id"] for s in accepted_after[:2]]
        on_main = user_id in main_uids
    else:
        on_main = len(accepted_after) > 0 and accepted_after[0]["user_id"] == user_id

    result = "accepted on " + class_name if on_main else "added as sub"
    await interaction.followup.send(
        "\u2705  " + username + " \u2014 " + result + ".", ephemeral=True
    )

    if match and user_id:
        thread_id = match["thread_id"]
        if thread_id:
            try:
                thread = interaction.client.get_channel(thread_id)
                if thread:
                    if on_main:
                        role_str = f"**{class_name}**"
                    else:
                        role_str = f"**{class_name}** (sub)"
                    await thread.send(f"<@{user_id}> you've been accepted as {role_str}! \u2705")
            except Exception:
                pass

    try:
        pending    = await signups_db.get_pending_signups(match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == class_name], key=lambda s: s["id"])
        if class_pend:
            view = PlayerPickView(match_id, class_name, class_pend)
            text = "**" + class_name + "**  \u2014  click a player to accept *(chronological order)*"
            await interaction.message.edit(content=text, view=view)
        else:
            view = await ReviewView.create(match_id)
            text, _ = await build_manage_text(match_id)
            await interaction.message.edit(content=text, view=view)
    except Exception:
        pass


class PlayerPickView(ui.View):
    """Shows pending players as plain buttons -- click to accept, no deny needed here."""
    def __init__(self, match_id, class_name, pending_signups):
        super().__init__(timeout=300)
        self.match_id   = match_id
        self.class_name = class_name
        row = 0
        for i, s in enumerate(pending_signups):
            if i > 0 and i % 5 == 0:
                row += 1
            if row > 3:
                break
            self.add_item(AcceptPlayerButton(match_id, s["id"], s["username"], class_name, row))
        self.add_item(BackToReviewButton(match_id, row=4))


class AcceptPlayerButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label=username,
            style=discord.ButtonStyle.success,
            custom_id="acc:" + str(match_id) + ":" + str(signup_id),
            row=row,
        )
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name

    async def callback(self, interaction):
        current = await signups_db.get_signup_by_id(self.signup_id)
        already = current and current["status"] == "accepted"

        if already:
            await interaction.response.send_message(
                f"\u274c **{self.username}** has already been accepted.", ephemeral=True
            )
            return

        filled  = await signups_db.count_accepted_for_class(self.match_id, self.class_name)

        user_id    = current["user_id"] if current else None
        player_lp  = await is_lp(interaction.client, user_id) if user_id else False
        if player_lp and not already:
            view = LPConfirmView(self.match_id, self.signup_id, self.username, self.class_name, filled)
            await interaction.response.send_message(
                "\u26a0\ufe0f **" + self.username + "** currently has the Low Priority role. Are you sure you want to accept them?",
                view=view, ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await _do_accept(interaction, self.match_id, self.signup_id, self.username, self.class_name, filled, already, current)


class DenyPlayerButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label="Deny  " + username,
            style=discord.ButtonStyle.danger,
            custom_id="den:" + str(match_id) + ":" + str(signup_id),
            row=row,
        )
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await signups_db.update_signup_status(self.signup_id, "denied")
        await refresh_message(interaction.client, self.match_id)
        await interaction.followup.send(
            "\u274c  " + self.username + " denied for " + self.class_name + ".", ephemeral=True
        )

        pending    = await signups_db.get_pending_signups(self.match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == self.class_name], key=lambda s: s["id"])
        if class_pend:
            view = PlayerPickView(self.match_id, self.class_name, class_pend)
            text = "**" + self.class_name + "**  \u2014  click a player to accept *(chronological order)*"
            await interaction.message.edit(content=text, view=view)
        else:
            view = await ReviewView.create(self.match_id)
            text, _ = await build_manage_text(self.match_id)
            await interaction.message.edit(content=text, view=view)


class BackToReviewButton(ui.Button):
    def __init__(self, match_id, row):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="back_review:" + str(match_id),
            row=row,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        view = await ReviewView.create(self.match_id)
        text, _ = await build_manage_text(self.match_id)
        await interaction.response.edit_message(content=text, view=view)


class ReviewView(ui.View):
    """The main review panel -- dropdown to pick a class."""
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id):
        self    = cls(match_id)
        match   = await matches_db.get_match(match_id)
        is_sixs = match["type"] in ("6s_opug", "6s_mix") if match else False
        pending = await signups_db.get_pending_signups(match_id)
        pending_by_class = {}
        for s in pending:
            pending_by_class.setdefault(s["class_name"], []).append(s)
        if pending_by_class:
            self.add_item(ClassDropdownSelect(match_id, pending_by_class, is_sixs=is_sixs))
        return self


# ── Deny review panel ─────────────────────────────────────────────────────────

class DenyClassDropdownSelect(ui.Select):
    def __init__(self, match_id, pending_by_class, is_sixs=False):
        self.match_id = match_id
        class_list    = SIXS_CLASSES if is_sixs else TF2_CLASSES
        options = []
        for cls in class_list:
            count = len(pending_by_class.get(cls, []))
            if count:
                options.append(discord.SelectOption(
                    label=cls,
                    value=cls,
                    description=str(count) + " pending",
                ))
        if not options:
            options = [discord.SelectOption(label="No pending", value="_none", description="No pending sign-ups")]
        super().__init__(
            placeholder="Select a class to deny from...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction):
        class_name = self.values[0]
        pending    = await signups_db.get_pending_signups(self.match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == class_name], key=lambda s: s["id"])

        if not class_pend:
            await interaction.response.edit_message(
                content="No pending sign-ups for **" + class_name + "** anymore.",
                view=await DenyReviewView.create(self.match_id),
            )
            return

        view = DenyPlayerPickView(self.match_id, class_name, class_pend)
        text = "**" + class_name + "**  \u2014  click a player to deny *(chronological order)*"
        await interaction.response.edit_message(content=text, view=view)


class DenyPlayerPickView(ui.View):
    def __init__(self, match_id, class_name, pending_signups):
        super().__init__(timeout=300)
        self.match_id   = match_id
        self.class_name = class_name
        row = 0
        for i, s in enumerate(pending_signups):
            if i > 0 and i % 5 == 0:
                row += 1
            if row > 3:
                break
            self.add_item(DenyOnlyButton(match_id, s["id"], s["username"], class_name, row))
        self.add_item(BackToDenyReviewButton(match_id, row=4))


class DenyOnlyButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label=username,
            style=discord.ButtonStyle.danger,
            custom_id="dny:" + str(match_id) + ":" + str(signup_id),
            row=row,
        )
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await signups_db.update_signup_status(self.signup_id, "denied")
        await refresh_message(interaction.client, self.match_id)
        await interaction.followup.send(
            "\u274c " + self.username + " denied for **" + self.class_name + "**.", ephemeral=True
        )
        pending    = await signups_db.get_pending_signups(self.match_id)
        class_pend = sorted([s for s in pending if s["class_name"] == self.class_name], key=lambda s: s["id"])
        try:
            if class_pend:
                view = DenyPlayerPickView(self.match_id, self.class_name, class_pend)
                text = "**" + self.class_name + "**  \u2014  click a player to deny *(chronological order)*"
            else:
                view = await DenyReviewView.create(self.match_id)
                text, _ = await build_manage_text(self.match_id)
            await interaction.edit_original_response(content=text, view=view)
        except Exception:
            pass


class BackToDenyReviewButton(ui.Button):
    def __init__(self, match_id, row):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="back_deny_review:" + str(match_id),
            row=row,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        view = await DenyReviewView.create(self.match_id)
        text, _ = await build_manage_text(self.match_id)
        await interaction.response.edit_message(content=text, view=view)


class DenyReviewView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id):
        self    = cls(match_id)
        match   = await matches_db.get_match(match_id)
        is_sixs = match["type"] in ("6s_opug", "6s_mix") if match else False
        pending = await signups_db.get_pending_signups(match_id)
        pending_by_class = {}
        for s in pending:
            pending_by_class.setdefault(s["class_name"], []).append(s)
        if pending_by_class:
            self.add_item(DenyClassDropdownSelect(match_id, pending_by_class, is_sixs=is_sixs))
        return self


# ── 6s Sign-up views ─────────────────────────────────────────────────────────

class SixsClassButton(ui.Button):
    def __init__(self, class_name, match_id):
        super().__init__(
            label=class_name,
            emoji=SIXS_CLASS_EMOJI[class_name],
            custom_id=f"sixs_signup:{match_id}:{class_name}",
            style=discord.ButtonStyle.secondary,
            row=SIXS_CLASSES.index(class_name) // 4,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)
        if not match or match["ended"]:
            await interaction.followup.send("This match has already ended.", ephemeral=True)
            return
        existing_class = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, self.class_name)
        if existing_class and existing_class["status"] == "cancelled":
            existing_class = None
        if existing_class:
            if existing_class["status"] == "denied":
                await interaction.followup.send(
                    f"You've been denied for **{self.class_name}**. Please contact the hoster.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(f"You're already signed up for **{self.class_name}**.", ephemeral=True)
            return

        all_signups = await signups_db.get_non_denied_signups_for_user(self.match_id, interaction.user.id)
        for s in all_signups:
            if s["status"] == "accepted":
                accepted_for = await signups_db.get_accepted_signups_for_class(self.match_id, s["class_name"])
                if accepted_for and accepted_for[0]["user_id"] == interaction.user.id:
                    await interaction.followup.send(
                        f"You're already on the main roster as **{s['class_name']}**. "
                        "Sign out first if you want to change classes.",
                        ephemeral=True,
                    )
                    return

        clashing = await signups_db.get_accepted_matches_for_user(
            interaction.user.id, exclude_match_id=self.match_id, reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(f"{m['team_name'] or 'a match'} (<#{m['channel_id']}>)" for m in clashing)
            view = ClashConfirmView(self.match_id, self.class_name, clash_names)
            await interaction.followup.send(
                "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names + ". Sign up anyway?",
                view=view, ephemeral=True
            )
            return
        await _do_signup(interaction, self.match_id, self.class_name)


class SixsSignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in SIXS_CLASSES:
            self.add_item(SixsClassButton(cls, match_id))
        self.add_item(SignOutButton(match_id))


# ── 6s Split view ─────────────────────────────────────────────────────────────

class SixsSwapClassButton(ui.Button):
    def __init__(self, class_name, match_id, row):
        super().__init__(
            label=class_name,
            emoji=SIXS_CLASS_EMOJI[class_name],
            style=discord.ButtonStyle.secondary,
            custom_id="sixs_swap:" + str(match_id) + ":" + class_name,
            row=row,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        split    = await matches_db.get_team_split(self.match_id)
        signups  = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]
        if not split:
            await interaction.response.send_message("No split data.", ephemeral=True)
            return
        red, blu = split["red"], split["blu"]
        red_s = [s for s in accepted if s["user_id"] in red and s["class_name"] == self.class_name]
        blu_s = [s for s in accepted if s["user_id"] in blu and s["class_name"] == self.class_name]
        if not red_s or not blu_s:
            await interaction.response.send_message("Can't swap \u2014 missing player on one side.", ephemeral=True)
            return
        red_uid, blu_uid = red_s[0]["user_id"], blu_s[0]["user_id"]
        new_red = [blu_uid if u == red_uid else u for u in red]
        new_blu = [red_uid if u == blu_uid else u for u in blu]
        await matches_db.save_team_split(self.match_id, new_red, new_blu)
        red_team = [s for s in accepted if s["user_id"] in new_red]
        blu_team = [s for s in accepted if s["user_id"] in new_blu]
        text = build_6s_split_view_text(red_team, blu_team)
        view = SixsSplitView(self.match_id, red_team, blu_team)
        await interaction.response.edit_message(content=text, view=view)


class SixsPostTeamsButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(label="Post teams", style=discord.ButtonStyle.success,
                         custom_id="sixs_post_teams:" + str(match_id), row=2)
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer()
        split    = await matches_db.get_team_split(self.match_id)
        signups  = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]
        match    = await matches_db.get_match(self.match_id)
        red_uids, blu_uids = split["red"], split["blu"]
        red_team = [s for s in accepted if s["user_id"] in red_uids]
        blu_team = [s for s in accepted if s["user_id"] in blu_uids]
        subs     = [s for s in accepted if s["user_id"] not in red_uids and s["user_id"] not in blu_uids]
        channel  = interaction.client.get_channel(match["channel_id"])
        if channel:
            await channel.send(build_6s_opug_teams_message(match, red_team, blu_team, subs))

        await matches_db.set_teams_posted(self.match_id)

        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.followup.send("\u2705 Teams posted!", ephemeral=True)


class SixsSplitView(ui.View):
    def __init__(self, match_id, red_team, blu_team):
        super().__init__(timeout=None)
        self.match_id = match_id
        for i, cls in enumerate(SIXS_CLASSES):
            row = i // 4
            self.add_item(SixsSwapClassButton(cls, match_id, row))
        self.add_item(SixsPostTeamsButton(match_id))


# ── OPUG Team Split ───────────────────────────────────────────────────────────

class SwapClassButton(ui.Button):
    def __init__(self, class_name, match_id, row):
        super().__init__(
            label=class_name,
            emoji=CLASS_EMOJI[class_name],
            style=discord.ButtonStyle.secondary,
            custom_id="swap:" + str(match_id) + ":" + class_name,
            row=row,
        )
        self.class_name = class_name
        self.match_id   = match_id

    async def callback(self, interaction):
        split = await matches_db.get_team_split(self.match_id)
        if not split:
            await interaction.response.send_message("No split data found.", ephemeral=True)
            return

        red = split["red"]
        blu = split["blu"]

        signups = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]

        red_signups = [s for s in accepted if s["user_id"] in red and s["class_name"] == self.class_name]
        blu_signups = [s for s in accepted if s["user_id"] in blu and s["class_name"] == self.class_name]

        if not red_signups or not blu_signups:
            await interaction.response.send_message(
                "Can't swap \u2014 one team has no player for " + self.class_name + ".", ephemeral=True
            )
            return

        red_uid = red_signups[0]["user_id"]
        blu_uid = blu_signups[0]["user_id"]

        new_red = [blu_uid if uid == red_uid else uid for uid in red]
        new_blu = [red_uid if uid == blu_uid else uid for uid in blu]

        await matches_db.save_team_split(self.match_id, new_red, new_blu)

        red_s = [s for s in accepted if s["user_id"] in new_red]
        blu_s = [s for s in accepted if s["user_id"] in new_blu]

        text = build_split_view_text(red_s, blu_s)
        view = SplitView(self.match_id, red_s, blu_s)
        await interaction.response.edit_message(content=text, view=view)


class PostTeamsButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(
            label="Post teams",
            style=discord.ButtonStyle.success,
            custom_id="post_teams:" + str(match_id),
            row=4,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer()
        split    = await matches_db.get_team_split(self.match_id)
        signups  = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]
        match    = await matches_db.get_match(self.match_id)

        red_uids = split["red"]
        blu_uids = split["blu"]

        red_team = []
        blu_team = []
        subs     = []
        for cls in TF2_CLASSES:
            cls_accepted = [s for s in accepted if s["class_name"] == cls]
            for s in cls_accepted:
                if s["user_id"] in red_uids:
                    red_team.append(s)
                elif s["user_id"] in blu_uids:
                    blu_team.append(s)
                else:
                    subs.append(s)

        channel = interaction.client.get_channel(match["channel_id"])
        if channel:
            msg_text = build_opug_teams_message(match, red_team, blu_team, subs)
            await channel.send(msg_text)

        await matches_db.set_teams_posted(self.match_id)

        try:
            await interaction.message.delete()
        except Exception:
            pass

        await interaction.followup.send("\u2705 Teams posted!", ephemeral=True)


class SplitView(ui.View):
    def __init__(self, match_id, red_team, blu_team):
        super().__init__(timeout=None)
        self.match_id = match_id
        for i, cls in enumerate(TF2_CLASSES):
            row = 2 + i // 5
            self.add_item(SwapClassButton(cls, match_id, row))
        self.add_item(PostTeamsButton(match_id))


# ── Fresh Pug Manage View ─────────────────────────────────────────────────────

class OPugCancelAfterStartView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, cancel anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        success = await do_cancel(interaction.client, self.match_id)
        if success:
            await interaction.followup.send("\u2705 Match cancelled.", ephemeral=True)
        else:
            await interaction.followup.send("\u274c Could not cancel.", ephemeral=True)

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Cancellation aborted.", view=None)


# ── Fresh PUG signup ──────────────────────────────────────────────────────────

async def refresh_fresh_pug_signup_list(client, match_id):
    """Edit the signup list message for a fresh pug to reflect current signups."""
    match = await matches_db.get_match(match_id)
    if not match:
        return
    signup_list_msg_id = match["signup_list_msg_id"]
    if not signup_list_msg_id:
        return
    channel = client.get_channel(match["channel_id"])
    if not channel:
        return
    try:
        signups = await signups_db.get_signups_for_match(match_id)
        msg     = await channel.fetch_message(signup_list_msg_id)
        await msg.edit(content=build_fresh_pug_signup_list(signups))
    except Exception as e:
        log.warning(f"refresh_fresh_pug_signup_list failed for match #{match_id}: {e}")


class FreshPugSignupButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(
            label="Sign Up",
            emoji=discord.PartialEmoji.from_str("<:PUG:1367589835874893885>"),
            custom_id=f"fp_signup:{match_id}",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This Fresh PUG has already ended or been cancelled.", ephemeral=True
            )
            return

        existing = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, "any")
        if existing and existing["status"] != "denied":
            await interaction.followup.send(
                "You're already signed up for this Fresh PUG.", ephemeral=True
            )
            return

        signup_id = await signups_db.add_signup(
            self.match_id, interaction.user.id,
            interaction.user.display_name, "any",
        )
        if signup_id is None:
            await interaction.followup.send("Could not sign up. Try again.", ephemeral=True)
            return

        await signups_db.update_signup_status(signup_id, "accepted")

        is_sixs = match["type"] == "6s_fresh_pug"
        cap     = 12 if is_sixs else 18
        signups = await signups_db.get_signups_for_match(self.match_id)
        count   = len([s for s in signups if s["status"] == "accepted"])

        if count == cap and config.HOSTER_CHANNEL_ID:
            hoster_ch = interaction.client.get_channel(config.HOSTER_CHANNEL_ID)
            if hoster_ch:
                mode = "6s Fresh PUG" if is_sixs else "Fresh PUG"
                try:
                    await hoster_ch.send(
                        f"<@{match['created_by']}> \U0001f389 **{mode}** is full! ({cap}/{cap})"
                    )
                except Exception:
                    pass

        await refresh_fresh_pug_signup_list(interaction.client, self.match_id)
        await interaction.followup.send("\u2705 You're signed up!", ephemeral=True)


class FreshPugSignOutButton(ui.Button):
    def __init__(self, match_id):
        super().__init__(
            label="Sign Out",
            emoji="\U0001f6aa",
            custom_id=f"fp_signout:{match_id}",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        self.match_id = match_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This Fresh PUG has already ended or been cancelled.", ephemeral=True
            )
            return

        existing = await signups_db.get_signup_by_user_and_class(self.match_id, interaction.user.id, "any")
        if not existing or existing["status"] == "denied":
            await interaction.followup.send(
                "You're not signed up for this Fresh PUG.", ephemeral=True
            )
            return

        await signups_db.remove_signup(self.match_id, interaction.user.id, "any")
        await refresh_fresh_pug_signup_list(interaction.client, self.match_id)
        await interaction.followup.send("You've been signed out.", ephemeral=True)


class FreshPugSignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.add_item(FreshPugSignupButton(match_id))
        self.add_item(FreshPugSignOutButton(match_id))


class FreshPugManageView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @ui.button(label="Conclude PUG", style=discord.ButtonStyle.success, row=0)
    async def conclude(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if time.time() < match["timestamp"]:
            remaining = int(match["timestamp"] - time.time())
            h, m = divmod(remaining // 60, 60)
            await interaction.response.send_message(
                f"\u274c You can only conclude after the PUG has started. Starts in **{h}h {m}m**.",
                ephemeral=True,
            )
            return
        view = FreshPugConcludeConfirmView(self.match_id)
        await interaction.response.send_message(
            "Conclude this Fresh PUG? This will archive the thread and clean up.",
            view=view, ephemeral=True,
        )

    @ui.button(label="Cancel PUG", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction, button):
        view = FreshPugCancelConfirmView(self.match_id)
        await interaction.response.send_message(
            "\u26a0\ufe0f Cancel this Fresh PUG?",
            view=view, ephemeral=True,
        )


class FreshPugConcludeConfirmView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, conclude", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        match   = await matches_db.get_match(self.match_id)

        # Fresh pug channels are dynamically created (no static pool to
        # return to, and no 24h notice step like mix/opug get) -- tear
        # down immediately rather than purge-and-leave.
        try:
            await channel_service.teardown_match_channels(interaction.guild, self.match_id)
        except Exception as e:
            log.warning(f"channel teardown failed for match #{self.match_id}: {e}")

        ongoing_channel_id = getattr(interaction.client, "ongoing_channel", None)
        if ongoing_channel_id and match["ongoing_msg_id"]:
            try:
                oc   = interaction.client.get_channel(ongoing_channel_id)
                omsg = await oc.fetch_message(match["ongoing_msg_id"])
                await omsg.delete()
            except Exception:
                pass

        await matches_db.end_match(self.match_id)

        matched_logs = None
        try:
            from pingu.services import log_service
            matched_logs = await log_service.find_and_attach_logs(match)
        except Exception as e:
            log.warning(f"logs.tf lookup failed for match #{self.match_id}: {e}")

        fire_archive_task(interaction.client, self.match_id, concluded=True,
                          hoster_channel_id=config.HOSTER_CHANNEL_ID,
                          triggered_by=interaction.user.id, matched_logs=matched_logs)
        await interaction.followup.send("\u2705 Fresh PUG concluded. Archiving in background...", ephemeral=True)

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class FreshPugCancelConfirmView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @ui.button(label="Yes, cancel", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        match   = await matches_db.get_match(self.match_id)

        try:
            await channel_service.teardown_match_channels(interaction.guild, self.match_id)
        except Exception as e:
            log.warning(f"channel teardown failed for match #{self.match_id}: {e}")

        ongoing_channel_id = getattr(interaction.client, "ongoing_channel", None)
        if ongoing_channel_id and match["ongoing_msg_id"]:
            try:
                oc   = interaction.client.get_channel(ongoing_channel_id)
                omsg = await oc.fetch_message(match["ongoing_msg_id"])
                await omsg.delete()
            except Exception:
                pass

        await matches_db.end_match(self.match_id)

        fire_archive_task(interaction.client, self.match_id, concluded=False,
                          hoster_channel_id=config.HOSTER_CHANNEL_ID,
                          triggered_by=interaction.user.id)
        await interaction.followup.send("\u2705 Fresh PUG cancelled. Archiving in background...", ephemeral=True)

    @ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Cancellation aborted.", view=None)


# ── Move to pending ───────────────────────────────────────────────────────────

class MoveToPendingClassSelect(ui.Select):
    def __init__(self, match_id, accepted_by_class, is_sixs=False):
        self.match_id = match_id
        class_list    = SIXS_CLASSES if is_sixs else TF2_CLASSES
        options = []
        for cls in class_list:
            count = len(accepted_by_class.get(cls, []))
            if count:
                options.append(discord.SelectOption(
                    label=cls,
                    value=cls,
                    description=f"{count} accepted",
                ))
        if not options:
            options = [discord.SelectOption(label="None", value="_none", description="No accepted players")]
        super().__init__(placeholder="Select a class\u2026", options=options, min_values=1, max_values=1)

    async def callback(self, interaction):
        class_name = self.values[0]
        if class_name == "_none":
            await interaction.response.edit_message(content="No accepted players.", view=None)
            return
        accepted = await signups_db.get_accepted_signups_for_class(self.match_id, class_name)
        if not accepted:
            await interaction.response.edit_message(
                content=f"No accepted players for **{class_name}** anymore.", view=None
            )
            return
        view = MoveToPendingPlayerView(self.match_id, class_name, accepted)
        await interaction.response.edit_message(
            content=f"**{class_name}** \u2014 select a player to move back to pending:",
            view=view
        )


class MoveToPendingPlayerButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label=username,
            style=discord.ButtonStyle.secondary,
            custom_id=f"mtp:{match_id}:{signup_id}",
            row=row,
        )
        self.match_id  = match_id
        self.signup_id = signup_id
        self.username  = username
        self.class_name = class_name

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)

        current = await signups_db.get_signup_by_id(self.signup_id)
        user_id = current["user_id"] if current else None

        await signups_db.move_accepted_to_pending(self.signup_id)
        if current:
            await signups_db.restore_cancelled_to_pending(self.match_id, current["user_id"])

        await reorder_class_roster(interaction.client, self.match_id, self.class_name)
        await refresh_message(interaction.client, self.match_id)

        if user_id:
            match = await matches_db.get_match(self.match_id)
            if match and match["thread_id"]:
                try:
                    thread = interaction.client.get_channel(match["thread_id"])
                    if thread:
                        await thread.send(
                            f"<@{user_id}> you've been moved back to pending by the hoster. "
                            "Please wait to be re-accepted."
                        )
                except Exception:
                    pass

        await interaction.followup.send(
            f"\u21a9\ufe0f **{self.username}** moved back to pending for **{self.class_name}**. "
            "Their other sign-ups have been restored.",
            ephemeral=True
        )

        try:
            accepted = await signups_db.get_accepted_signups_for_class(self.match_id, self.class_name)
            if accepted:
                view = MoveToPendingPlayerView(self.match_id, self.class_name, accepted)
                await interaction.message.edit(
                    content=f"**{self.class_name}** \u2014 select a player to move back to pending:",
                    view=view
                )
            else:
                await interaction.message.edit(
                    content=f"No more accepted players for **{self.class_name}**.", view=None
                )
        except Exception:
            pass


class MoveToPendingPlayerView(ui.View):
    def __init__(self, match_id, class_name, accepted_signups):
        super().__init__(timeout=300)
        self.match_id   = match_id
        self.class_name = class_name
        row = 0
        for i, s in enumerate(accepted_signups):
            if i > 0 and i % 5 == 0:
                row += 1
            if row > 3:
                break
            self.add_item(MoveToPendingPlayerButton(match_id, s["id"], s["username"], class_name, row))


class MoveToPendingView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id, is_sixs=False):
        self    = cls(match_id)
        accepted = await signups_db.get_accepted_signups(match_id)
        accepted_by_class = {}
        for s in accepted:
            accepted_by_class.setdefault(s["class_name"], []).append(s)
        self.add_item(MoveToPendingClassSelect(match_id, accepted_by_class, is_sixs=is_sixs))
        return self


# ── Restore denied ────────────────────────────────────────────────────────────

class RestoreDeniedClassSelect(ui.Select):
    def __init__(self, match_id, denied_by_class, is_sixs=False):
        self.match_id = match_id
        class_list    = SIXS_CLASSES if is_sixs else TF2_CLASSES
        options = []
        for cls in class_list:
            count = len(denied_by_class.get(cls, []))
            if count:
                options.append(discord.SelectOption(
                    label=cls,
                    value=cls,
                    description=f"{count} denied",
                ))
        if not options:
            options = [discord.SelectOption(label="None", value="_none", description="No denied players")]
        super().__init__(placeholder="Select a class\u2026", options=options, min_values=1, max_values=1)

    async def callback(self, interaction):
        class_name = self.values[0]
        if class_name == "_none":
            await interaction.response.edit_message(content="No denied players.", view=None)
            return
        signups = await signups_db.get_signups_for_match(self.match_id)
        denied  = [s for s in signups if s["status"] == "denied" and s["class_name"] == class_name]
        if not denied:
            await interaction.response.edit_message(
                content=f"No denied players for **{class_name}** anymore.", view=None
            )
            return
        view = RestoreDeniedPlayerView(self.match_id, class_name, denied)
        await interaction.response.edit_message(
            content=f"**{class_name}** \u2014 select a player to restore to pending:",
            view=view
        )


class RestoreDeniedPlayerButton(ui.Button):
    def __init__(self, match_id, signup_id, username, class_name, row):
        super().__init__(
            label=username,
            style=discord.ButtonStyle.success,
            custom_id=f"rden:{match_id}:{signup_id}",
            row=row,
        )
        self.match_id   = match_id
        self.signup_id  = signup_id
        self.username   = username
        self.class_name = class_name

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await signups_db.update_signup_status(self.signup_id, "pending")
        await refresh_message(interaction.client, self.match_id)

        await interaction.followup.send(
            f"\u21a9\ufe0f **{self.username}** restored to pending for **{self.class_name}**.",
            ephemeral=True
        )

        try:
            signups = await signups_db.get_signups_for_match(self.match_id)
            denied  = [s for s in signups if s["status"] == "denied" and s["class_name"] == self.class_name]
            if denied:
                view = RestoreDeniedPlayerView(self.match_id, self.class_name, denied)
                await interaction.message.edit(
                    content=f"**{self.class_name}** \u2014 select a player to restore to pending:",
                    view=view
                )
            else:
                await interaction.message.edit(
                    content=f"No more denied players for **{self.class_name}**.", view=None
                )
        except Exception:
            pass


class RestoreDeniedPlayerView(ui.View):
    def __init__(self, match_id, class_name, denied_signups):
        super().__init__(timeout=300)
        self.match_id   = match_id
        self.class_name = class_name
        row = 0
        for i, s in enumerate(denied_signups):
            if i > 0 and i % 5 == 0:
                row += 1
            if row > 3:
                break
            self.add_item(RestoreDeniedPlayerButton(match_id, s["id"], s["username"], class_name, row))


class RestoreDeniedView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id, is_sixs=False):
        self    = cls(match_id)
        signups = await signups_db.get_signups_for_match(self.match_id)
        denied  = [s for s in signups if s["status"] == "denied"]
        denied_by_class = {}
        for s in denied:
            denied_by_class.setdefault(s["class_name"], []).append(s)
        self.add_item(RestoreDeniedClassSelect(match_id, denied_by_class, is_sixs=is_sixs))
        return self


class SlimManageView(ui.View):
    """Minimal manage view for 8h reminders -- Conclude and Cancel only."""
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    @ui.button(label="Conclude match", style=discord.ButtonStyle.success, row=0)
    async def conclude_match(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if time.time() < match["timestamp"]:
            remaining = int(match["timestamp"] - time.time())
            h, m = divmod(remaining // 60, 60)
            await interaction.response.send_message(
                f"\u274c You can only conclude after the match has started. Starts in **{h}h {m}m**.",
                ephemeral=True,
            )
            return
        if match["type"] in ("opug", "6s_opug"):
            split = await matches_db.get_team_split(self.match_id)
            if not split:
                await interaction.response.send_message(
                    "\u274c Teams haven't been split yet. Use **Split teams** in /manage first.",
                    ephemeral=True,
                )
                return
            teams_posted = match["teams_posted"]
            if not teams_posted:
                await interaction.response.send_message(
                    "\u274c Teams have been split but not posted yet. Press **Post teams** in the balancing chat first.",
                    ephemeral=True,
                )
                return
        view = ConcludeConfirmView(self.match_id)
        await interaction.response.send_message(
            "Ready to conclude this match?", view=view, ephemeral=True
        )

    @ui.button(label="Cancel match", style=discord.ButtonStyle.danger, row=0)
    async def cancel_match(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if match and match["type"] in ("opug", "6s_opug") and time.time() > match["timestamp"]:
            view = OPugCancelAfterStartView(self.match_id)
            await interaction.response.send_message(
                "\u26a0\ufe0f This PUG has already started. If the match was played, use **Conclude** instead.\n"
                "Are you sure you want to cancel?",
                view=view, ephemeral=True,
            )
            return
        view = CancelConfirmView(self.match_id)
        await interaction.response.send_message(
            "\u26a0\ufe0f Are you sure you want to cancel this match?",
            view=view, ephemeral=True,
        )


class ManageView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=300)
        self.match_id = match_id

    @classmethod
    async def create(cls, match_id):
        return cls(match_id)

    @ui.button(label="Accept players", style=discord.ButtonStyle.primary, row=0)
    async def review_pending(self, interaction, button):
        pending = await signups_db.get_pending_signups(self.match_id)
        if not pending:
            await interaction.response.send_message("No pending sign-ups right now.", ephemeral=True)
            return
        text, _ = await build_manage_text(self.match_id)
        view     = await ReviewView.create(self.match_id)
        await interaction.response.send_message(text, view=view, ephemeral=True)

    @ui.button(label="Deny players", style=discord.ButtonStyle.danger, row=0)
    async def deny_players(self, interaction, button):
        pending = await signups_db.get_pending_signups(self.match_id)
        if not pending:
            await interaction.response.send_message("No pending sign-ups to deny right now.", ephemeral=True)
            return
        text, _ = await build_manage_text(self.match_id)
        view     = await DenyReviewView.create(self.match_id)
        await interaction.response.send_message(text, view=view, ephemeral=True)

    @ui.button(label="Move to pending", style=discord.ButtonStyle.secondary, row=0)
    async def move_to_pending(self, interaction, button):
        match   = await matches_db.get_match(self.match_id)
        accepted = await signups_db.get_accepted_signups(self.match_id)
        if not accepted:
            await interaction.response.send_message("No accepted players right now.", ephemeral=True)
            return
        is_sixs    = match["type"] in ("6s_mix", "6s_opug") if match else False
        view = await MoveToPendingView.create(self.match_id, is_sixs=is_sixs)
        await interaction.response.send_message(
            "Select a class to move a player back to pending:", view=view, ephemeral=True
        )

    @ui.button(label="Restore denied", style=discord.ButtonStyle.secondary, row=0)
    async def restore_denied(self, interaction, button):
        match   = await matches_db.get_match(self.match_id)
        signups = await signups_db.get_signups_for_match(self.match_id)
        denied  = [s for s in signups if s["status"] == "denied"]
        if not denied:
            await interaction.response.send_message("No denied players right now.", ephemeral=True)
            return
        is_sixs = match["type"] in ("6s_mix", "6s_opug") if match else False
        view = await RestoreDeniedView.create(self.match_id, is_sixs=is_sixs)
        await interaction.response.send_message(
            "Select a class to restore a denied player to pending:", view=view, ephemeral=True
        )

    @ui.button(label="Conclude match", style=discord.ButtonStyle.success, row=1)
    async def conclude_match(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if time.time() < match["timestamp"]:
            remaining = int(match["timestamp"] - time.time())
            h, m = divmod(remaining // 60, 60)
            await interaction.response.send_message(
                f"\u274c You can only conclude after the match has started. "
                f"Starts in **{h}h {m}m**.",
                ephemeral=True,
            )
            return
        if match["type"] in ("opug", "6s_opug"):
            split = await matches_db.get_team_split(self.match_id)
            if not split:
                await interaction.response.send_message(
                    "\u274c Teams haven't been split yet. Use **Split teams** first, then post the teams before concluding.",
                    ephemeral=True,
                )
                return
            teams_posted = match["teams_posted"]
            if not teams_posted:
                await interaction.response.send_message(
                    "\u274c Teams have been split but not posted yet. Press **Post teams** in the balancing chat before concluding.",
                    ephemeral=True,
                )
                return
        view = ConcludeConfirmView(self.match_id)
        await interaction.response.send_message(
            "Ready to conclude this match? This will post a conclusion notice, "
            "archive the thread, and log it to the archive channel.",
            view=view, ephemeral=True,
        )

    @ui.button(label="Split teams", style=discord.ButtonStyle.primary, row=1)
    async def split_teams(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if not match or match["type"] not in ("opug", "6s_opug"):
            await interaction.response.send_message(
                "\u274c Team splitting is only available for Organised PUGs.", ephemeral=True
            )
            return

        signups  = await signups_db.get_signups_for_match(self.match_id)
        accepted = [s for s in signups if s["status"] == "accepted"]

        is_sixs     = match["type"] == "6s_opug"
        class_list  = SIXS_CLASSES if is_sixs else TF2_CLASSES
        cap         = 12 if is_sixs else 18
        slot_counts = {}
        for s in accepted:
            slot_counts[s["class_name"]] = slot_counts.get(s["class_name"], 0) + 1
        total = sum(min(v, 2) for v in slot_counts.values())

        if total < cap:
            await interaction.response.send_message(
                f"\u274c Not all {cap} slots are filled yet ({total}/{cap}). Please wait until all players have signed up.",
                ephemeral=True,
            )
            return

        red_uids = []
        blu_uids = []
        for cls in class_list:
            cls_players = [s for s in accepted if s["class_name"] == cls][:2]
            if len(cls_players) >= 1:
                red_uids.append(cls_players[0]["user_id"])
            if len(cls_players) >= 2:
                blu_uids.append(cls_players[1]["user_id"])

        await matches_db.save_team_split(self.match_id, red_uids, blu_uids)

        red_team = [s for s in accepted if s["user_id"] in red_uids]
        blu_team = [s for s in accepted if s["user_id"] in blu_uids]

        if is_sixs:
            text = build_6s_split_view_text(red_team, blu_team)
            view = SixsSplitView(self.match_id, red_team, blu_team)
        else:
            text = build_split_view_text(red_team, blu_team)
            view = SplitView(self.match_id, red_team, blu_team)

        if config.BALANCING_CHAT_ID:
            bal_ch = interaction.client.get_channel(config.BALANCING_CHAT_ID)
            if bal_ch:
                await bal_ch.send(text, view=view)
                await interaction.response.send_message(
                    f"\u2705 Split posted to {bal_ch.mention}!", ephemeral=True
                )
                return
        await interaction.response.send_message(text, view=view, ephemeral=True)

    @ui.button(label="Cancel match", style=discord.ButtonStyle.danger, row=1)
    async def cancel_match(self, interaction, button):
        match = await matches_db.get_match(self.match_id)
        if match and match["type"] in ("opug", "6s_opug") and time.time() > match["timestamp"]:
            view = OPugCancelAfterStartView(self.match_id)
            await interaction.response.send_message(
                "\u26a0\ufe0f This PUG has already started. If the match was played, use **Conclude** instead.\n"
                "Are you sure you want to cancel?",
                view=view, ephemeral=True,
            )
            return
        view = CancelConfirmView(self.match_id)
        await interaction.response.send_message(
            "\u26a0\ufe0f Are you sure you want to cancel this match?\n"
            "This will delete the embed, ping accepted players with a 24h notice, "
            "and archive the thread.",
            view=view, ephemeral=True,
        )
