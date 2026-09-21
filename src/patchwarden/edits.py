"""SEARCH/REPLACE edit blocks: the only way the Fixer changes code.

    app/utils.py
    <<<<<<< SEARCH
    exact lines to find
    =======
    lines to put there instead
    >>>>>>> REPLACE

The search text must occur exactly once. Anything else is an error, never a guess.
"""

import re

from patchwarden.models import EditBlock
from patchwarden.workspace import Workspace, WorkspaceError

SEARCH, DIVIDER, REPLACE = "<<<<<<< SEARCH", "=======", ">>>>>>> REPLACE"


class EditParseError(ValueError):
    pass


class EditApplyError(ValueError):
    """kind: bad_path | empty_search | no_match | ambiguous."""

    def __init__(self, kind: str, block: EditBlock, detail: str = ""):
        self.kind = kind
        self.block = block
        super().__init__(f"{kind}: {block.file}" + (f" ({detail})" if detail else ""))


def parse_edit_blocks(text: str) -> list[EditBlock]:
    lines = text.splitlines()
    blocks: list[EditBlock] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() != SEARCH:
            i += 1
            continue
        # The path is the nearest non-blank line above, skipping a ```python fence.
        above = (ln.strip() for ln in reversed(lines[:i]))
        path = next((ln for ln in above if ln and not ln.startswith("```")), "").strip("`")
        if not path or path == REPLACE:
            raise EditParseError(f"edit block at line {i + 1} has no file path above it")
        try:
            mid = lines.index(DIVIDER, i + 1)
            end = lines.index(REPLACE, mid + 1)
        except ValueError:
            raise EditParseError(f"unterminated edit block for {path}") from None
        blocks.append(
            EditBlock(
                file=path,
                search=_join(lines[i + 1 : mid]),
                replace=_join(lines[mid + 1 : end]),
            )
        )
        i = end + 1
    if not blocks and SEARCH in text:
        raise EditParseError("malformed edit block")
    return blocks


def _join(lines: list[str]) -> str:
    return "".join(ln + "\n" for ln in lines)


def apply_edits(ws: Workspace, blocks: list[EditBlock]) -> list[str]:
    """Apply blocks in order, each to the text left by the previous one. All or nothing: on
    any error nothing is written. Returns the files changed."""
    pending: dict[str, str] = {}
    for block in blocks:
        try:
            ok = ws.exists(block.file)
        except WorkspaceError:
            ok = False
        if not ok:
            raise EditApplyError("bad_path", block, "not a Python file in the repo")
        text = pending.get(block.file, ws.read(block.file))
        pending[block.file] = apply_block(text, block)
    for rel, text in pending.items():
        ws.write(rel, text)
    return list(pending)


def apply_block(text: str, block: EditBlock) -> str:
    if not block.search.strip():
        raise EditApplyError("empty_search", block)
    n = text.count(block.search)
    if n == 1:
        return text.replace(block.search, block.replace, 1)
    if n > 1:
        raise EditApplyError("ambiguous", block, f"search text occurs {n} times")
    # One fallback: the model often drops or adds trailing whitespace.
    spans = _loose_matches(text, block.search)
    if len(spans) == 1:
        start, end = spans[0]
        return text[:start] + block.replace + text[end:]
    if spans:
        raise EditApplyError("ambiguous", block, f"search text occurs {len(spans)} times")
    raise EditApplyError("no_match", block, "search text not found")


def _loose_matches(text: str, search: str) -> list[tuple[int, int]]:
    """Spans of whole-line runs equal to `search` when trailing whitespace is ignored."""
    want = [ln.rstrip() for ln in search.splitlines()]
    lines = re.findall(r"[^\n]*\n|[^\n]+$", text)
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))
    got = [ln.rstrip() for ln in lines]
    return [
        (offsets[i], offsets[i + len(want)])
        for i in range(len(lines) - len(want) + 1)
        if got[i : i + len(want)] == want
    ]
