"""Split out of views/legacy.py -- the sign-up flow (mix, opug, 6s mix),
clash detection, and the host-roster @mention block check.

Also holds the "All" convenience buttons (sign up for every class in one
click) and the full Open For All state machine (direct-accept, no hoster
approval, at-most-one-main-slot-plus-one-queued-sub-while-locked, with
cascading promotion on release -- see do_signout in signout_views.py for
the cascade itself).
"""

import discord
from discord import ui

from pingu.embeds import (
    TF2_CLASSES, CLASS_EMOJI, SIXS_CLASSES, SIXS_CLASS_EMOJI,
)
from pingu.templates.emojis import ALL_CLASSES_EMOJI
from pingu import config
from pingu.db import matches as matches_db
from pingu.db import signups as signups_db
from pingu.services import roster_service
from pingu.views.signout_views import SignOutButton, do_signout


def _is_mix_banned(interaction: discord.Interaction) -> bool:
    """Checked at the top of every sign-up button's callback -- covers
    mix, oPUG, and 6s alike (Mix Ban is meant to keep someone out of
    organized games generally, not one specific match type). Does NOT
    cover a hoster manually @mentioning a banned player into the free-
    text host roster field -- that's a different code path entirely
    (parsed text, not a button click), out of scope here."""
    if not config.MIX_BAN_ROLE_ID:
        return False
    return any(r.id == config.MIX_BAN_ROLE_ID for r in interaction.user.roles)


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

        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
            )
            return

        if interaction.user.id in roster_service.host_roster_user_ids(match["host_roster"]):
            await interaction.followup.send(
                "You're already on the host team roster for this match.", ephemeral=True
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

async def _notify_hoster_of_clash(interaction, match_id, class_name):
    """
    Shared by _do_signup and _do_open_for_all_signup -- pings the
    hoster(s) of both matches when someone signs up for one while already
    accepted in another close enough in time to clash. Previously this
    only existed inside _do_signup (used by regular oPUG/mix), so Open
    For All signups never triggered it at all -- not because of which
    PUG came first, but because whichever signup went through
    _do_open_for_all_signup specifically never ran this check, regardless
    of direction.
    """
    match = await matches_db.get_match(match_id)
    if not match:
        return

    clashing = await signups_db.get_accepted_matches_for_user(
        interaction.user.id,
        exclude_match_id=match_id,
        reference_timestamp=match["timestamp"]
    )
    if not clashing or not config.HOSTER_CHANNEL_ID:
        return

    hoster_ch = interaction.client.get_channel(config.HOSTER_CHANNEL_ID)
    if not hoster_ch:
        return

    match_type = match["type"]
    if match_type in ("opug", "6s_opug"):
        this_match_label = f"{match['division'] or 'PUG'} PUG"
    elif match_type == "6s_mix":
        this_match_label = f"{match['team_name'] or 'Mix'} vs Mix 6s"
    else:
        this_match_label = f"{match['team_name'] or 'Mix'} vs Mix"

    hoster_pings = {match["created_by"]}
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
        f"in <#{match['channel_id']}> ({this_match_label}) "
        f"but is already accepted in {clash_refs}."
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

    # No confirmation message shown. This is a button click (a "component"
    # interaction) -- interaction.response.defer() for those ALWAYS uses
    # Discord's DEFERRED_MESSAGE_UPDATE type regardless of the ephemeral
    # flag passed (that flag only means anything for slash-command-style
    # interactions). Which means there was never an ephemeral placeholder
    # to dismiss in the first place -- a bare defer() already shows the
    # clicking user nothing at all for a button. The delete_original_
    # response() call that used to be here was therefore operating on the
    # PUBLIC match message itself (since that's what "original response"
    # resolves to for a deferred component interaction), silently
    # deleting the real mix message every time someone signed up. Do not
    # re-add a delete/edit of the original response here.

    await _notify_hoster_of_clash(interaction, match_id, class_name)

    interaction.client.ui_updater.schedule_refresh(match_id)

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

        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
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
        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
            )
            return
        if interaction.user.id in roster_service.host_roster_user_ids(match["host_roster"]):
            await interaction.followup.send(
                "You're already on the host team roster for this match.", ephemeral=True
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


# --- "All" convenience button: mix, regular oPUG, and their 6s variants
# (NOT Open For All -- see OpenForAllAllButton below for that one) ---

async def _do_all_signup(interaction, match_id, class_list):
    """Signs the clicker up for every class in class_list in one action --
    same outcome as clicking each class button individually, batched into
    one refresh instead of one per class. Skips classes they're already
    signed up for rather than erroring the whole batch over one."""
    user_id  = interaction.user.id
    username = interaction.user.display_name

    signed_up = []
    already   = []
    for cls in class_list:
        existing = await signups_db.get_signup_by_user_and_class(match_id, user_id, cls)
        if existing and existing["status"] not in ("cancelled",):
            already.append(cls)
            continue
        signup_id = await signups_db.add_signup(match_id, user_id, username, cls)
        if signup_id is not None:
            signed_up.append(cls)
        else:
            already.append(cls)

    interaction.client.ui_updater.schedule_refresh(match_id)

    if signed_up:
        msg = f"\u2705 Signed up for: {', '.join(signed_up)}."
        if already:
            msg += f"\nAlready signed up for: {', '.join(already)}."
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.followup.send("You're already signed up for every class.", ephemeral=True)


class AllClashConfirmView(ui.View):
    def __init__(self, match_id, class_list, clash_names):
        super().__init__(timeout=60)
        self.match_id   = match_id
        self.class_list = class_list
        self.clash_names = clash_names

    @ui.button(label="Yes, sign up anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await _do_all_signup(interaction, self.match_id, self.class_list)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Sign-up cancelled.", view=None)


class AllClassButton(ui.Button):
    """Sign up for every class in one click -- mix, regular oPUG, and
    their 6s variants. No gating (unlike Open For All's equivalent) --
    signups here always land as pending for a hoster to review, so there's
    no direct-accept correctness concern the way there is for Open For All.
    """

    def __init__(self, match_id, class_list, row, check_host_roster=False):
        super().__init__(
            label="All",
            emoji=ALL_CLASSES_EMOJI,
            custom_id=f"signup_all:{match_id}",
            style=discord.ButtonStyle.primary,
            row=row,
        )
        self.match_id = match_id
        self.class_list = class_list
        self.check_host_roster = check_host_roster

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This match has already ended or been cancelled.", ephemeral=True
            )
            return

        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
            )
            return

        if self.check_host_roster and interaction.user.id in roster_service.host_roster_user_ids(match["host_roster"]):
            await interaction.followup.send(
                "You're already on the host team roster for this match.", ephemeral=True
            )
            return

        clashing = await signups_db.get_accepted_matches_for_user(
            interaction.user.id, exclude_match_id=self.match_id, reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(
                f"{m['team_name'] or 'a mix'} (<#{m['channel_id']}>)" for m in clashing
            )
            view = AllClashConfirmView(self.match_id, self.class_list, clash_names)
            warn = (
                "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names +
                ". Sign up for all classes anyway?"
            )
            await interaction.followup.send(warn, view=view, ephemeral=True)
            return

        await _do_all_signup(interaction, self.match_id, self.class_list)


class OPugSignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in TF2_CLASSES:
            self.add_item(OPugClassButton(cls, match_id))
        self.add_item(AllClassButton(match_id, TF2_CLASSES, row=1))
        self.add_item(SignOutButton(match_id))

class SignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in TF2_CLASSES:
            self.add_item(ClassButton(cls, match_id))
        self.add_item(AllClassButton(match_id, TF2_CLASSES, row=1, check_host_roster=True))
        self.add_item(SignOutButton(match_id))

class SixsSignupView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        for cls in SIXS_CLASSES:
            self.add_item(SixsClassButton(cls, match_id))
        self.add_item(AllClassButton(match_id, SIXS_CLASSES, row=1, check_host_roster=True))
        self.add_item(SignOutButton(match_id))

# --- Open For All oPUG: direct-accept, no hoster approval step ---
#
# Full state machine (confirmed in chat):
#   UNLOCKED (holds no main slot):
#     - click an open class  -> become main there, now LOCKED
#     - click a full class   -> join its sub queue; can hold MULTIPLE
#       queued full-class spots simultaneously while still unlocked
#   LOCKED (holds exactly one main slot):
#     - click current class      -> no-op, already signed up
#     - click a different OPEN class -> immediate move: release old
#       main (cascades -- see do_signout), claim new, still locked
#     - click a different FULL class -> join its sub queue, REPLACING
#       any sub-queue spot already held (capped at exactly one while
#       locked) -- current main is untouched until/unless promoted
#     - gets promoted (queued class opens, their turn) -> old main
#       releases (cascading), new main claimed, still locked
#
# Race safety: every state-changing attempt goes through
# signups_db.try_direct_accept() (atomic, BEGIN IMMEDIATE) FIRST: only
# after it reports success do we release anything the player already
# held. This ordering means a lost race (someone else claims the last
# spot between our read and our attempt) never costs a player their
# existing spot -- try_direct_accept re-validates against live state
# regardless of what we believed going in.

async def _get_ofa_state(match_id, user_id):
    """Returns (main_signup_row_or_None, sub_signup_row_or_None) for a
    player's current Open For All state. main = an accepted signup where
    they're in the top-2 (rostered) for that class. sub = any OTHER
    accepted signup they hold (at most one while locked, by design)."""
    all_signups = await signups_db.get_non_denied_signups_for_user(match_id, user_id)
    accepted = [s for s in all_signups if s["status"] == "accepted"]
    main = None
    sub = None
    for s in accepted:
        accepted_for_class = await signups_db.get_accepted_signups_for_class(match_id, s["class_name"])
        main_uids = [a["user_id"] for a in accepted_for_class[:2]]
        if user_id in main_uids:
            main = s
        else:
            sub = s
    return main, sub


async def _do_open_for_all_signup(interaction, match_id, class_name):
    user_id  = interaction.user.id
    username = interaction.user.display_name

    main, sub = await _get_ofa_state(match_id, user_id)

    if main and main["class_name"] == class_name:
        await interaction.followup.send(f"You're already signed up for **{class_name}**.", ephemeral=True)
        return
    if sub and sub["class_name"] == class_name:
        await interaction.followup.send(f"You're already queued as a sub for **{class_name}**.", ephemeral=True)
        return

    # Always attempt the new class as MAIN first, regardless of locked/
    # unlocked state -- and only release anything already held AFTER
    # this succeeds. If the class turns out to be full (or we lose a
    # last-spot race), nothing they already had gets touched.
    result, _ = await signups_db.try_direct_accept(match_id, user_id, username, class_name, cap=2)
    if result == "accepted":
        if main:
            await do_signout(interaction.client, match_id, user_id, main["class_name"])
        await _notify_hoster_of_clash(interaction, match_id, class_name)
        interaction.client.ui_updater.schedule_refresh(match_id)
        await interaction.followup.send(f"\u2705 You're in as **{class_name}**!", ephemeral=True)
        return
    if result == "already_signed_up":
        await interaction.followup.send(f"You're already signed up for **{class_name}**.", ephemeral=True)
        return

    # class is full -- queue as sub instead.
    if main:
        # LOCKED: cap of exactly one queued sub-class -- replace whichever
        # one they already held, if any.
        if sub:
            await signups_db.remove_signup(match_id, user_id, sub["class_name"])
        result2, _ = await signups_db.try_direct_accept(match_id, user_id, username, class_name, cap=4)
        if result2 == "accepted":
            await _notify_hoster_of_clash(interaction, match_id, class_name)
            interaction.client.ui_updater.schedule_refresh(match_id)
            note = f" (dropped your **{sub['class_name']}** sub queue)" if sub else ""
            await interaction.followup.send(
                f"\u2705 Queued as a sub for **{class_name}**{note}. "
                f"You'll move here if a slot opens \u2014 still on **{main['class_name']}** for now.",
                ephemeral=True,
            )
        else:
            interaction.client.ui_updater.schedule_refresh(match_id)
            await interaction.followup.send(
                f"**{class_name}**'s sub queue is full too \u2014 still on **{main['class_name']}**.",
                ephemeral=True,
            )
    else:
        # UNLOCKED: no cap -- can hold multiple queued full-class spots
        # simultaneously, whichever opens first claims them.
        result2, _ = await signups_db.try_direct_accept(match_id, user_id, username, class_name, cap=4)
        if result2 == "accepted":
            await _notify_hoster_of_clash(interaction, match_id, class_name)
            interaction.client.ui_updater.schedule_refresh(match_id)
            await interaction.followup.send(
                f"\u2705 Queued as a sub for **{class_name}**. You'll move here if a slot opens.",
                ephemeral=True,
            )
        elif result2 == "already_signed_up":
            await interaction.followup.send(f"You're already queued as a sub for **{class_name}**.", ephemeral=True)
        else:
            await interaction.followup.send(f"**{class_name}**'s sub queue is full too.", ephemeral=True)


class OpenForAllClashConfirmView(ui.View):
    def __init__(self, match_id, class_name, clash_names):
        super().__init__(timeout=60)
        self.match_id    = match_id
        self.class_name  = class_name
        self.clash_names = clash_names

    @ui.button(label="Yes, sign up anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await _do_open_for_all_signup(interaction, self.match_id, self.class_name)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Sign-up cancelled.", view=None)


class OpenForAllClassButton(ui.Button):
    """Parametrized on class_list/emoji_map rather than duplicated per
    HL/6s -- Open For All applies to both, and the callback logic is
    otherwise identical between them.

    Deliberately does NOT block on "already on the main roster elsewhere"
    the way OPugClassButton/SixsClassButton do -- that block is exactly
    what the state machine above needs to NOT have, since moving between
    classes while locked is the whole point.
    """

    def __init__(self, class_name, match_id, class_list, emoji_map, row_size):
        super().__init__(
            label=class_name,
            emoji=emoji_map[class_name],
            custom_id=f"ofa_signup:{match_id}:{class_name}",
            style=discord.ButtonStyle.secondary,
            row=class_list.index(class_name) // row_size,
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

        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
            )
            return

        clashing = await signups_db.get_accepted_matches_for_user(
            interaction.user.id, exclude_match_id=self.match_id, reference_timestamp=match["timestamp"]
        )
        if clashing:
            clash_names = ", ".join(
                f"{m['team_name'] or 'a mix'} (<#{m['channel_id']}>)" for m in clashing
            )
            view = OpenForAllClashConfirmView(self.match_id, self.class_name, clash_names)
            warn = (
                "\u26a0\ufe0f **Warning:** You are already accepted in " + clash_names +
                ". Are you sure you want to sign up for this PUG too?"
            )
            await interaction.followup.send(warn, view=view, ephemeral=True)
            return

        await _do_open_for_all_signup(interaction, self.match_id, self.class_name)


class OpenForAllAllButton(ui.Button):
    """Open For All's "All" button -- distinct from AllClassButton above,
    since it needs real gating: blocked outright if the clicker already
    holds a main slot (they can only queue one other class at a time,
    same as clicking a single full class while locked), and blocked
    entirely until the roster is genuinely full (every class at its
    2-main cap) -- with no hoster to decide who plays what, there'd be no
    way to know which class an early "All" click should actually place
    someone in. Once the roster's full, this just queues them as a sub
    for every class simultaneously -- the flex-sub case."""

    def __init__(self, match_id, class_list, emoji_map, row):
        super().__init__(
            label="All",
            emoji=ALL_CLASSES_EMOJI,
            custom_id=f"ofa_signup_all:{match_id}",
            style=discord.ButtonStyle.primary,
            row=row,
        )
        self.match_id = match_id
        self.class_list = class_list

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        match = await matches_db.get_match(self.match_id)

        if not match or match["ended"]:
            await interaction.followup.send(
                "This PUG has already ended or been cancelled.", ephemeral=True
            )
            return

        if _is_mix_banned(interaction):
            await interaction.followup.send(
                "\u274c You currently have a Mix Ban and can't sign up for matches.", ephemeral=True
            )
            return

        user_id = interaction.user.id
        main, sub = await _get_ofa_state(self.match_id, user_id)

        if main:
            await interaction.followup.send(
                f"You're already on the main roster for **{main['class_name']}** \u2014 "
                f"you can only pick 1 other class to sub.",
                ephemeral=True,
            )
            return

        class_counts = [await signups_db.count_accepted_for_class(self.match_id, cls) for cls in self.class_list]
        roster_full = all(c >= 2 for c in class_counts)
        if not roster_full:
            await interaction.followup.send(
                "The roster isn't full yet \u2014 please pick just one class to sign up for.",
                ephemeral=True,
            )
            return

        username = interaction.user.display_name
        queued = []
        for cls in self.class_list:
            result, _ = await signups_db.try_direct_accept(self.match_id, user_id, username, cls, cap=4)
            if result == "accepted":
                queued.append(cls)

        interaction.client.ui_updater.schedule_refresh(self.match_id)

        if queued:
            await interaction.followup.send(
                f"\u2705 Queued as a sub for: {', '.join(queued)}.", ephemeral=True
            )
        else:
            await interaction.followup.send("Every sub queue is already full.", ephemeral=True)


class OpenForAllSignupView(ui.View):
    def __init__(self, match_id, is_sixs=False):
        super().__init__(timeout=None)
        class_list = SIXS_CLASSES if is_sixs else TF2_CLASSES
        emoji_map  = SIXS_CLASS_EMOJI if is_sixs else CLASS_EMOJI
        row_size   = 4 if is_sixs else 5
        for cls in class_list:
            self.add_item(OpenForAllClassButton(cls, match_id, class_list, emoji_map, row_size))
        self.add_item(OpenForAllAllButton(match_id, class_list, emoji_map, row=1))
        self.add_item(SignOutButton(match_id))