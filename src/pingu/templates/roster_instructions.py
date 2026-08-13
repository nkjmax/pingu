"""
The "how to type your roster" text -- one shared source, used by both a
hoster's post-creation prompt (typed directly in the match channel) and a
mix-request captain's prompt (typed in the request thread). Previously
duplicated with the requester's version missing the example line
entirely -- this is the fix for that gap.
"""

HL_EXAMPLE = "@kaldoz, @aboood, @aswero21, @mackey, mugen, @surge, tbc, tbc, tbc"
SIXS_EXAMPLE = "@kaldoz, @aboood, @aswero21, mugen, @surge, tbc"


def roster_instructions_block(is_sixs: bool = False) -> str:
    """The reusable instructional body -- list format, class order,
    example. Callers prepend their own context-specific lead-in line
    ("type your roster in {channel}" vs "post your roster below")."""
    if is_sixs:
        return (
            "List 6 players separated by commas in class order "
            "(PScout, FScout, PSoldier, Roamer, Demo, Medic).\n"
            "@mentions and plain text both work.\n"
            f"Example: `{SIXS_EXAMPLE}`"
        )
    return (
        "List 9 players separated by commas in class order (Scout to Spy).\n"
        "@mentions and plain text both work.\n"
        f"Example: `{HL_EXAMPLE}`"
    )
