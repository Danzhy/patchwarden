"""Prompt files, one per role, plus the helpers that wrap repo content for them.

`prompt_version()` goes into every run row: the manual PROMPT_VERSION plus a hash of the prompt
files, so a prompt edit shows up in the trace even if nobody bumps the number.
"""

import hashlib
import re
from functools import cache
from importlib.resources import files

from patchwarden.workspace import split_lines

PROMPT_VERSION = "m3.1"
ROLES = ("triage", "fixer")
TAG = "untrusted_repo_content"
_CLOSE = re.compile(rf"</\s*{TAG}", re.IGNORECASE)


@cache
def load(role: str) -> str:
    return files(__package__).joinpath(f"{role}.md").read_text(encoding="utf-8")


def prompt_version() -> str:
    h = hashlib.sha256()
    for role in ROLES:
        h.update(load(role).encode())
    return f"{PROMPT_VERSION}+{h.hexdigest()[:8]}"


def untrusted(text: str, source: str) -> str:
    """Repo text inside delimiters. A closing tag inside the text is escaped, so a file can't
    end the untrusted block early and put its own instructions after it."""
    body = _CLOSE.sub(lambda m: m.group(0).replace("</", "<\\/"), text)
    if not body.endswith("\n"):
        body += "\n"
    # The path is repo content too: a file named `x"><instructions>.py` is legal.
    src = source.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    src = src.replace(">", "&gt;")
    return f'<{TAG} source="{src}">\n{body}</{TAG}>'


def numbered_window(text: str, start: int, end: int, radius: int = 20) -> tuple[str, int, int]:
    """Lines start-radius..end+radius (1-based), numbered, with ">" on the finding's lines.
    Returns (text, first line, last line)."""
    lines = [ln.rstrip("\r\n") for ln in split_lines(text)]
    lo = max(1, start - radius)
    hi = min(len(lines), end + radius)
    width = len(str(hi))
    out = [
        f"{'>' if start <= n <= end else ' '} {n:>{width}} | {lines[n - 1]}"
        for n in range(lo, hi + 1)
    ]
    return "\n".join(out), lo, hi
