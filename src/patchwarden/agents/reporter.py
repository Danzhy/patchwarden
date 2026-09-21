"""Reporter: plain templates, no LLM. `scan` gets a text report, `fix` a markdown report with
Auto-fixed / Suggested / Escalated / False-positive sections (also the future PR comment)."""

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


def _line(o: FindingOutcome) -> str:
    return f"`{o.finding.location}` **{o.finding.rule_id}**: {o.finding.message}"


def _diff_block(diff: str) -> list[str]:
    return ["", "  ```diff", *("  " + ln for ln in diff.rstrip("\n").splitlines()), "  ```"]


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
        how = "ruff (safe fix)" if o.fixed_by == "ruff" else f"Fixer: {o.rationale or o.reason}"
        out.append(f"- {_line(o)} — {how}")

    out += ["", f"## Suggested, needs your OK ({len(by[FindingStatus.suggested])})", ""]
    if not by[FindingStatus.suggested]:
        out.append("None.")
    for o in by[FindingStatus.suggested]:
        out.append(f"- {_line(o)}")
        out.append(f"  Why not automatic: {o.reason}")
        if o.rationale:
            out.append(f"  Proposed fix: {o.rationale}")
        if o.diff:
            out += _diff_block(o.diff)

    out += ["", f"## Escalated ({len(human)})", ""]
    if not human:
        out.append("None.")
    for o in human:
        out.append(f"- {_line(o)}")
        label = "Could not fix" if o.status == FindingStatus.failed else "Why"
        out.append(f"  {label}: {o.reason}")
        if o.triage and o.triage.risk_notes:
            out.append(f"  Proposed approach / risks: {o.triage.risk_notes}")
        for v in o.violations:
            out.append(f"  Rejected fix: {v.kind} ({v.file}: {v.detail})")

    fps = by[FindingStatus.false_positive]
    if fps:
        out += ["", f"## Marked false positive ({len(fps)})", ""]
        out += [f"- {_line(o)} — {o.reason}" for o in fps]
    left = by[FindingStatus.not_triaged]
    if left:
        out += ["", f"## Not triaged ({len(left)})", ""]
        out += [f"- {_line(o)} ({o.preclass.kind})" for o in left]
    return "\n".join(out) + "\n"
