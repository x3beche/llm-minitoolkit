"""Patterns that strip personal details from imported text.

Copy this to redactions.py and put your own in. It is imported by
import_site.py and is deliberately not committed: the whole point of the file
is that it contains the details you do not want published.

Each entry is (compiled pattern, replacement). They run in order, over the
title, the summary and the markdown body of every article. Anything that
matches is gone before it reaches the database.
"""
import re

REDACTIONS = [
    # A banner or signature block that runs to the end of a quoted string
    (re.compile(r"◆\s*YOUR\s+NAME\s*◆[^\"]*?(?=\"|$)", re.I), ""),

    # Addresses and profiles
    (re.compile(r"you@example\.com", re.I), "[email removed]"),
    (re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/yourhandle/?", re.I), "[link removed]"),

    # Home directory paths that leak a username
    (re.compile(r"/home/yourname\b"), "/home/user"),

    # A name in the places code tends to carry one
    (re.compile(r'(MODULE_AUTHOR\s*\(\s*")Your Name(")'), r"\1Example Author\2"),
    (re.compile(r'(author:\s*\\?")Your Name(\\?")'), r"\1Example Author\2"),
    (re.compile(r"Your\s+Name", re.I), "Example Author"),

    # An employer or institution, to the end of the sentence
    (re.compile(r"\bYOUR\s+EMPLOYER\b[^.\n]*", re.I), ""),
]

# Example addresses in security notes and systemd units like serial-getty@ are
# left alone on purpose, as are project links and long hex numbers - those are
# clock frequencies and register dumps, not phone numbers.
