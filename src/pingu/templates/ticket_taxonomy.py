"""
The /ticket taxonomy -- a tree of DELIBERATELY variable depth, matching
the actual design (not every branch needs the same number of steps):

  Report Player -> Conduct / Leadership Conduct -> leaf types   (3 levels)
  Appeal        -> leaf types directly                          (2 levels)
  Feedback      -> leaf types directly                          (2 levels)
  Other         -> itself is the leaf, no drill-down at all      (1 level)

Each top-level category maps to one of three shapes, and the dropdown
flow in cogs/tickets.py branches based on which shape it sees:
  - dict  -> show a subcategory dropdown next
  - list  -> show a type dropdown directly (subcategory step skipped)
  - None  -> skip straight to the description modal (no further dropdown)
"""

TICKET_TREE = {
    "Report Player": {
        "Conduct": ["Cheating/Alting", "Excessive Toxicity", "Harassment", "Other"],
        "Leadership Conduct": ["Hoster Conduct", "Admin/Mod Conduct"],
    },
    "Appeal": ["Appeal LP", "Appeal Ban"],
    "Feedback": ["Suggestion", "Report Bug", "Other"],
    "Other": None,
}

# Short codes for the ticket number format (CAT-YYYYMMDD-NN), separate from
# the tree's display names since "Report Player" doesn't fit that format.
CATEGORY_CODES = {
    "Report Player": "REPORT",
    "Appeal": "APPEAL",
    "Feedback": "FEEDBACK",
    "Other": "OTHER",
}