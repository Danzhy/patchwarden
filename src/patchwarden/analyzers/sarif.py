"""SARIF 2.1.0 -> Finding.

What the tools actually emit (NOTES.md, M0): ruff gives absolute `file://` URIs and no snippet;
bandit gives cwd-relative URIs with a snippet; CodeQL gives `%SRCROOT%`-relative URIs, may omit
`endLine`, and is the only one with `partialFingerprints`.
"""

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

from patchwarden.models import Finding, Region

_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def base_fingerprint(
    tool: str, rule_id: str, file: str, snippet: str, line_hash: str | None
) -> str:
    """No line numbers go in, so a finding keeps its fingerprint when code above it moves."""
    return _hash(tool, rule_id, file, line_hash or normalize_ws(snippet))


def relative_path(uri: str, repo_root: Path) -> str | None:
    """Repo-relative POSIX path for a SARIF artifact URI, or None if it's outside the repo."""
    if uri.startswith("file:"):
        path = Path(unquote(urlparse(uri).path))
    else:
        path = repo_root / unquote(uri)
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def _read_lines(repo_root: Path, rel: str, cache: dict[str, list[str]]) -> list[str]:
    if rel not in cache:
        try:
            cache[rel] = (repo_root / rel).read_text(errors="replace").splitlines(keepends=True)
        except OSError:
            cache[rel] = []
    return cache[rel]


def parse_sarif(doc: dict, tool: str, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    lines_cache: dict[str, list[str]] = {}
    for run in doc.get("runs", []):
        for res in run.get("results", []):
            f = _parse_result(res, tool, repo_root, lines_cache)
            if f is not None:
                findings.append(f)
    return _dedupe_fingerprints(findings)


def _parse_result(
    res: dict, tool: str, repo_root: Path, lines_cache: dict[str, list[str]]
) -> Finding | None:
    locs = res.get("locations") or []
    if not locs or "physicalLocation" not in locs[0]:
        return None
    phys = locs[0]["physicalLocation"]
    rel = relative_path(phys.get("artifactLocation", {}).get("uri", ""), repo_root)
    if rel is None:
        return None
    reg = phys.get("region", {})
    start = reg.get("startLine", 1)
    region = Region(
        start_line=start,
        end_line=reg.get("endLine", start),
        start_col=reg.get("startColumn"),
        end_col=reg.get("endColumn"),
    )
    snippet = reg.get("snippet", {}).get("text")
    if snippet is None:
        lines = _read_lines(repo_root, rel, lines_cache)
        snippet = "".join(lines[region.start_line - 1 : region.end_line])

    rule = res.get("ruleId") or res.get("rule", {}).get("id") or "unknown"
    rule_id = f"{tool}:{rule}"
    props = res.get("properties", {})
    line_hash = res.get("partialFingerprints", {}).get("primaryLocationLineHash")
    return Finding(
        tool=tool,
        rule_id=rule_id,
        message=res.get("message", {}).get("text", ""),
        file=rel,
        region=region,
        snippet=snippet,
        fingerprint=base_fingerprint(tool, rule_id, rel, snippet, line_hash),
        severity=(props.get("issue_severity") or res.get("level") or "").lower() or None,
        confidence=(props.get("issue_confidence") or "").lower() or None,
        tool_fixable=bool(res.get("fixes")),
    )


def _dedupe_fingerprints(findings: list[Finding]) -> list[Finding]:
    """Identical rule+snippet twice in one file: the 2nd, 3rd... (by line) get ":2", ":3".

    The first occurrence keeps the bare fingerprint, so adding a duplicate later doesn't change
    the fingerprint of the existing finding.
    """
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        groups[f.fingerprint].append(f)
    for group in groups.values():
        group.sort(key=lambda f: (f.region.start_line, f.region.start_col or 0))
        for n, f in enumerate(group[1:], start=2):
            f.fingerprint = f"{f.fingerprint}:{n}"
    return sorted(findings, key=lambda f: (f.file, f.region.start_line, f.rule_id))
