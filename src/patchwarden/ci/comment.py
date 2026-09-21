"""The single patchwarden comment on a PR: created once, then updated in place on every run.

The report quotes repo content and model output, so the body is treated as untrusted text:
@mentions outside code are defused (anyone could plant one to ping a person or team), and it's
cut to GitHub's size limit. Mentions inside code aren't notifications, and changing them there
would corrupt the suggested diffs (`@property`), so those are left alone.
"""

import re

import httpx

MARKER = "<!-- patchwarden -->"
MAX_BODY = 65_536  # GitHub's limit for a comment body, in characters
_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")
_INLINE_CODE = re.compile(r"(`+)(?:.+?)\1")
_FENCE = re.compile(r"\s*(`{3,}|~{3,})")
_REPO = re.compile(r"[\w.-]+/[\w.-]+")


class CommentError(RuntimeError):
    pass


def _defuse_line(line: str) -> str:
    out, pos = [], 0
    for m in _INLINE_CODE.finditer(line):
        out += [_MENTION.sub("@​", line[pos : m.start()]), m.group(0)]
        pos = m.end()
    out.append(_MENTION.sub("@​", line[pos:]))
    return "".join(out)


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
    budget = limit - len(MARKER) - 1 - len(footer) - len(cut_note) - 8  # 8: a closing fence

    lines, fence, size, cut = [], None, 0, False
    for line in report.splitlines():
        m = _FENCE.match(line)
        if fence is None:
            line = _defuse_line(line) if not m else line
            if m:
                fence = m.group(1)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
            if not line.strip().strip(fence[0]):  # a bare closing fence
                fence = None
        if size + len(line) + 1 > budget:
            cut = True
            break
        lines.append(line)
        size += len(line) + 1
    body = "\n".join(lines)
    if cut:
        if fence is not None:
            body += "\n" + fence
        body += cut_note
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
