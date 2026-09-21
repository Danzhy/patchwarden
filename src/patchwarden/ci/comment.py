"""The single patchwarden comment on a PR: created once, then updated in place on every run.

The report quotes repo content and model output, so the body is treated as untrusted text:
@mentions (which ping a person or team) and #123 references (which put a "mentioned this"
event on that issue) are defused everywhere except in fenced code, and the body is cut to
GitHub's size limit. Fenced code is left alone because GitHub doesn't link there, and changing
it would corrupt the suggested diffs (`@property`).

Fences are tracked conservatively: when unsure whether GitHub still sees a code block, the line
is treated as text. Inline code spans get no exception; their rules (exact backtick runs,
backslash escapes, spans across lines) are too easy to get subtly wrong, and a zero-width space
in an inline `@name` costs nothing.
"""

import re

import httpx

MARKER = "<!-- patchwarden -->"
MAX_BODY = 65_536  # GitHub's limit for a comment body, in characters
_MENTION = re.compile(r"(?<![A-Za-z0-9])@(?=[A-Za-z0-9])")  # not an email's @
_ISSUE_REF = re.compile(r"(?<!&)#(?=\d)")  # #12, o/r#12; not an HTML entity like &#64;
_FENCE = re.compile(r"(`{3,}|~{3,})(.*)")
_REPO = re.compile(r"(?!\.+/)[\w.-]+/(?!\.+$)[\w.-]+")  # owner/name, neither "." nor ".."
_ZWSP = "\u200b"


class CommentError(RuntimeError):
    pass


def _defuse(line: str) -> str:
    return _ISSUE_REF.sub("#" + _ZWSP, _MENTION.sub("@" + _ZWSP, line))


def _indent(line: str) -> tuple[int, str]:
    """(columns of leading whitespace, tabs to 4 like CommonMark, the rest of the line)."""
    rest = line.lstrip(" \t")
    return len(line[: len(line) - len(rest)].expandtabs(4)), rest


def _opens(line: str) -> tuple[str, int] | None:
    """(fence, indent) if `line` opens a fenced code block: at most 3 spaces of indent, and a
    backtick fence's info string has no backtick (otherwise it's inline code, not a fence)."""
    col, rest = _indent(line)
    m = _FENCE.fullmatch(rest)
    if not m or col > 3 or (m.group(1)[0] == "`" and "`" in m.group(2)):
        return None
    return m.group(1), col


def _still_inside(line: str, fence: tuple[str, int]) -> bool | None:
    """True: code. False: the closing fence. None: GitHub's block has ended some other way (a
    line indented less than the fence leaves the list item it was in), or might have."""
    run, at = fence
    col, rest = _indent(line)
    if rest and col < at:
        return None
    m = _FENCE.fullmatch(rest)
    if m and m.group(1)[0] == run[0] and len(m.group(1)) >= len(run) and not m.group(2).strip():
        # Up to 3 columns past the fence closes it in GitHub; closing early is the safe side.
        return col > at + 3
    return True


def render_body(report: str, run_url: str | None = None, limit: int = MAX_BODY) -> str:
    """MARKER + the report with mentions defused, cut at a line boundary to fit `limit`."""
    footer = "\n\n---\n"
    if run_url:
        footer += (
            f"The patch and the full trace are in the `patchwarden` artifact of [this run]"
            f"({run_url}); apply the auto-fixes with `git apply patchwarden.patch`.\n"
        )
    footer += "<sub>patchwarden: policy in code, every step traced.</sub>\n"
    cut_note = "\n\n*Report truncated to fit a GitHub comment; the full report is in the artifact.*"
    budget = limit - len(MARKER) - 1 - len(footer) - len(cut_note)

    def closer(f: tuple[str, int] | None) -> str:
        return "\n" + " " * f[1] + f[0] if f else ""

    lines, fence, size, cut = [], None, 0, False
    for line in report.splitlines():
        after = fence
        inside = _still_inside(line, fence) if fence else None
        if inside is False:
            after = None
        elif inside is None:
            after = _opens(line)
            if after is None:
                line = _defuse(line)
        # Room is kept for closing the fence this line leaves open, if the next one is cut.
        if size + len(line) + 1 + len(closer(after)) > budget:
            cut = True
            break
        lines.append(line)
        size += len(line) + 1
        fence = after
    body = "\n".join(lines)
    if cut:
        body += closer(fence) + cut_note
    return f"{MARKER}\n{body.rstrip()}{footer}"


class GitHub:
    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ):
        self.http = httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "patchwarden",
            },
            timeout=30,
            transport=transport,
        )

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise CommentError(f"GitHub API {method} {path}: {e}") from e
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = resp.text[:200]
            hint = (
                " (the token can't write to this PR: a fork PR, or the job lacks"
                " `permissions: pull-requests: write`)"
                if resp.status_code in (401, 403, 404)
                else ""
            )
            raise CommentError(
                f"GitHub API {method} {path}: HTTP {resp.status_code} {message}{hint}"
            )
        return resp

    def close(self) -> None:
        self.http.close()


def _ours(c: dict) -> bool:
    """Our earlier comment: a bot's (so not someone quoting the marker) that starts with it."""
    return (c.get("body") or "").startswith(MARKER) and (c.get("user") or {}).get("type") == "Bot"


def upsert_comment(gh: GitHub, repo: str, pr: int, body: str) -> tuple[str, str]:
    """Update our comment on the PR, or create it. ("created" | "updated", its URL)."""
    if not _REPO.fullmatch(repo):
        raise CommentError(f"not an owner/name repository: {repo!r}")
    existing, page = None, 1
    while existing is None:
        resp = gh.request(
            "GET", f"/repos/{repo}/issues/{pr}/comments", params={"per_page": 100, "page": page}
        )
        batch = resp.json()
        existing = next((c for c in batch if _ours(c)), None)
        if len(batch) < 100:
            break
        page += 1
    if existing:
        resp = gh.request(
            "PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", json={"body": body}
        )
        return "updated", resp.json().get("html_url", "")
    resp = gh.request("POST", f"/repos/{repo}/issues/{pr}/comments", json={"body": body})
    return "created", resp.json().get("html_url", "")
