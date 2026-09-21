"""Reporter: plain templates, no LLM. `scan` gets a text report, `fix` a markdown report with
Auto-fixed / Suggested / Escalated / False-positive sections (also the future PR comment)."""

import re
from collections import Counter

from patchwarden.models import FindingOutcome, FindingStatus, PreClassKind, ScanResult

# (kind, section title, short label for the summary line)
_GROUPS = [
    (PreClassKind.always_escalate, "Escalate: security / always-escalate rule", "escalate"),
    (PreClassKind.protected_path, "Escalate: protected path", "protected"),
    (
        PreClassKind.auto_fix_allowed,
        "Auto-fix candidates (still verified before applying)",
        "auto-fix",
    ),
    (PreClassKind.llm_decides, "Triage decides", "triage"),
]


def render_scan_text(result: ScanResult) -> str:
    counts = Counter(result.preclass[f.fingerprint].kind for f in result.findings)
    out = [
        f"patchwarden scan: {len(result.findings)} findings in {len(result.files_scanned)} files"
        f" ({result.scope_mode} scope; analyzers: {', '.join(result.analyzers_run) or 'none'})",
    ]
    for name, why in result.analyzers_skipped.items():
        out.append(f"  skipped {name}: {why}")
    if not result.findings:
        out.append("No findings.")
        return "\n".join(out) + "\n"
    out.append("  " + ", ".join(f"{label}: {counts[k]}" for k, _, label in _GROUPS if counts[k]))
    width = max(len(f.location) for f in result.findings)
    rwidth = max(len(f.rule_id) for f in result.findings)
    for kind, title, _ in _GROUPS:
        group = [f for f in result.findings if result.preclass[f.fingerprint].kind == kind]
        if not group:
            continue
        out += ["", f"{title} [{len(group)}]"]
        for f in group:
            pc = result.preclass[f.fingerprint]
            line = f"  {f.location:<{width}}  {f.rule_id:<{rwidth}}  {f.message}"
            if kind == PreClassKind.protected_path or "test file" in pc.reason:
                line += f"  [{pc.reason}]"
            out.append(line)
    return "\n".join(out) + "\n"


def _inline(text: str, limit: int = 500) -> str:
    """Model and analyzer text on one line: it can't start a heading, list or fence of its own
    in the report (the future PR comment), and `<` can't open HTML such as a fake marker."""
    one = " ".join(text.split()).replace("<", "&lt;")
    return one if len(one) <= limit else one[:limit].rstrip() + "…"


def _line(o: FindingOutcome) -> str:
    return f"`{o.finding.location}` **{o.finding.rule_id}**: {_inline(o.finding.message)}"


def _diff_block(diff: str) -> list[str]:
    # Longer than any backtick run in the diff, so the diff can't close the fence.
    runs = [len(m) for m in re.findall(r"`+", diff)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    body = ("  " + ln for ln in diff.rstrip("\n").split("\n"))
    return ["", f"  {fence}diff", *body, f"  {fence}"]


def render_fix_markdown(outcomes: list[FindingOutcome], run: dict) -> str:
    """`run`: run_id, outcome, cost_usd, patch (path or "")."""
    by = {s: [o for o in outcomes if o.status == s] for s in FindingStatus}
    human = by[FindingStatus.escalated] + by[FindingStatus.failed]
    out = [
        "# patchwarden report",
        "",
        f"Run `{run['run_id']}`: {len(outcomes)} findings; "
        f"{len(by[FindingStatus.fixed])} auto-fixed, {len(by[FindingStatus.suggested])} "
        f"suggested, {len(human)} escalated, {len(by[FindingStatus.false_positive])} false "
        f"positive"
        + (
            f", {len(by[FindingStatus.not_triaged])} not triaged (--no-llm)"
            if by[FindingStatus.not_triaged]
            else ""
        )
        + f". LLM cost ${run['cost_usd']:.4f}. Outcome: {run['outcome']}.",
        "",
        "> Fixes passed patchwarden's diff policy (no suppressions, no test or other-file edits, "
        "size and signature limits). They are not yet re-scanned or tested (coming in M4).",
    ]

    out += ["", f"## Auto-fixed ({len(by[FindingStatus.fixed])})", ""]
    if not by[FindingStatus.fixed]:
        out.append("None.")
    elif run.get("patch"):
        out.append(f"All of these are in `{run['patch']}`.")
        out.append("")
    for o in by[FindingStatus.fixed]:
        how = (
            "ruff (safe fix)"
            if o.fixed_by == "ruff"
            else f"Fixer: {_inline(o.rationale or o.reason)}"
        )
        out.append(f"- {_line(o)} — {how}")

    out += ["", f"## Suggested, needs your OK ({len(by[FindingStatus.suggested])})", ""]
    if not by[FindingStatus.suggested]:
        out.append("None.")
    for o in by[FindingStatus.suggested]:
        out.append(f"- {_line(o)}")
        out.append(f"  Why not automatic: {_inline(o.reason)}")
        if o.rationale:
            out.append(f"  Proposed fix: {_inline(o.rationale)}")
        if o.diff:
            out += _diff_block(o.diff)

    out += ["", f"## Escalated ({len(human)})", ""]
    if not human:
        out.append("None.")
    for o in human:
        out.append(f"- {_line(o)}")
        label = "Could not fix" if o.status == FindingStatus.failed else "Why"
        out.append(f"  {label}: {_inline(o.reason)}")
        if o.triage and o.triage.risk_notes:
            out.append(f"  Proposed approach / risks: {_inline(o.triage.risk_notes)}")
        for v in o.violations:
            out.append(f"  Rejected fix: {v.kind} ({v.file}: {_inline(v.detail)})")

    fps = by[FindingStatus.false_positive]
    if fps:
        out += ["", f"## Marked false positive ({len(fps)})", ""]
        out += [f"- {_line(o)} — {_inline(o.reason)}" for o in fps]
    left = by[FindingStatus.not_triaged]
    if left:
        out += ["", f"## Not triaged ({len(left)})", ""]
        out += [f"- {_line(o)} ({o.preclass.kind})" for o in left]
    return "\n".join(out) + "\n"
