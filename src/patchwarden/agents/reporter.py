"""Reporter. M1: a plain template for `scan`. The LLM-free fix report follows in M3-M4."""

from collections import Counter

from patchwarden.models import PreClassKind, ScanResult

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
