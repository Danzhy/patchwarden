"""Plain-text views of the trace store: `patchwarden trace list | show | stats`."""

import json
from collections import Counter, defaultdict

from patchwarden.models import VerifyResult
from patchwarden.tracing.store import TraceStore


def _load(text: str | None) -> dict:
    value = json.loads(text) if text else None
    return value if isinstance(value, dict) else {}


def resolve_run(store: TraceStore, ref: str) -> str | None:
    """A run id from "last", a full id or a unique prefix."""
    if ref == "last":
        row = store.db.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    ids = [
        r[0]
        for r in store.db.execute(
            "SELECT run_id FROM runs WHERE run_id LIKE ? ORDER BY run_id", (ref + "%",)
        )
    ]
    if ref in ids:
        return ref
    return ids[0] if len(ids) == 1 else None


def render_list(store: TraceStore, limit: int = 20) -> str:
    runs = store.db.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    if not runs:
        return "No runs.\n"
    count = Counter(r[0] for r in store.db.execute("SELECT run_id FROM findings"))
    flags = Counter(r[0] for r in store.db.execute("SELECT run_id FROM flags"))
    header = f"{'run':<22}  {'started (UTC)':<16}  {'trigger':<7}  {'outcome':<15}"
    out = [header + "  findings  flags  cost"]
    for r in runs:
        out.append(
            f"{r['run_id']:<22}  {(r['started_at'] or '')[:16].replace('T', ' '):<16}  "
            f"{r['trigger'] or '':<7}  {r['outcome'] or 'running':<15}  "
            f"{count[r['run_id']]:>8}  {flags[r['run_id']]:>5}  ${r['cost_usd'] or 0:.4f}"
        )
    return "\n".join(out) + "\n"


def _summary(step: dict) -> str:
    """One line on what a step did, from its stored input and output."""
    node, inp, out = step["node"], _load(step["input_json"]), _load(step["output_json"])
    if node.startswith("llm:"):
        tokens = f"{step['tokens_in']}→{step['tokens_out']} tok"
        return f"{step['model']}  {tokens}  ${step['cost'] or 0:.4f}"
    if node == "scan":
        return f"{out.get('findings')} findings in {out.get('files')} files"
    if node == "tests":
        return f"{inp.get('stage')}: {out.get('status', '?')}"
    if node == "deterministic":
        reverted = out.get("reverted") or {}
        return f"ruff resolved {len(out.get('resolved', []))}" + (
            f", reverted {len(reverted)} file(s)" if reverted else ""
        )
    if node == "triage":
        return f"{out.get('decision', '')} ({out.get('confidence', '')})" if out else ""
    if node == "clamp":
        return f"{out.get('decision')}" + (" (raised by policy)" if out.get("clamped") else "")
    if node == "fixer":
        return f"attempt {inp.get('attempt')}"
    if node == "apply_edits":
        return ", ".join(out.get("files", []))
    if node == "check_diff":
        kinds = [v["kind"] for v in out.get("violations", [])]
        return "VIOLATION " + ", ".join(kinds) if kinds else "clean"
    if node == "verify" and out:
        res = VerifyResult.model_validate(out)
        return (
            f"passed (tests {res.tests})" if res.passed else "FAILED: " + "; ".join(res.failures())
        )
    if node == "verifier" and out:
        return f"{out.get('verdict')}, {out.get('behaviour_change_risk')} risk: {out.get('reason')}"
    if node in ("finding", "finalize"):
        limit = " (round limit hit)" if out.get("round_limit_hit") else ""
        return f"-> {out.get('status', '?')}{limit}"
    return ""


def _clip(text: str, width: int = 160) -> str:
    """One line (indentation kept), cut at `width`."""
    first, *rest = str(text).splitlines() or [""]
    one = " ".join([first.rstrip(), *(part.strip() for part in rest)]).rstrip()
    return one if len(one) <= width else one[: width - 1] + "…"


def render_show(store: TraceStore, run_id: str) -> str:
    [run] = store.rows("runs", run_id)
    steps = sorted(store.rows("steps", run_id), key=lambda s: s["step_id"])
    findings = {f["finding_id"]: f for f in store.rows("findings", run_id)}
    flags = store.rows("flags", run_id)
    models = _load(run["models_json"])
    secs = (run["duration_ms"] or 0) / 1000
    out = [
        f"run {run['run_id']}  {run['trigger']}  {run['outcome'] or 'running'}  "
        f"${run['cost_usd'] or 0:.4f}  {secs:.1f}s  tokens {run['tokens_in']}→{run['tokens_out']}",
        f"repo {run['repo']}  git {(run['git_sha'] or '-')[:10]}  prompts {run['prompt_version']}",
        "models " + ", ".join(f"{k}={v}" for k, v in models.items()),
        "",
    ]

    def label(fid: str | None) -> str:
        f = findings.get(fid)
        return f"{f['rule_id']} {f['file']}:{f['line']}" if f else ""

    children: dict[int | None, list[dict]] = defaultdict(list)
    for s in steps:
        children[s["parent_step_id"]].append(s)

    def walk(parent: int | None, depth: int) -> None:
        for s in children[parent]:
            name = s["node"] + (f"  {label(s['finding_id'])}" if s["node"] == "finding" else "")
            line = f"{s['step_id']:>4} {'  ' * depth}{name}  {_summary(s)}".rstrip()
            out.append(f"{_clip(line)}  [{(s['latency_ms'] or 0) / 1000:.1f}s]")
            if s["error"]:
                out.append(f"     {'  ' * depth}! {_clip(s['error'])}")
            walk(s["step_id"], depth + 1)

    walk(None, 0)
    out += ["", f"Flags ({len(flags)})" + (":" if flags else "")]
    for f in flags:
        where = label(f["finding_id"]) or "run"
        out.append(_clip(f"  {f['detector']}  {where}  {f['detail']}"))
    return "\n".join(out) + "\n"


def render_stats(store: TraceStore, run_id: str | None = None) -> str:
    runs = store.rows("runs", run_id)
    findings = store.rows("findings", run_id)
    steps = store.rows("steps", run_id)
    flags = store.rows("flags", run_id)
    cost = sum(r["cost_usd"] or 0 for r in runs)
    out = [f"{len(runs)} run(s), {len(findings)} findings, LLM cost ${cost:.4f}", ""]

    out.append("Flags:" if flags else "Flags: none")
    by_detector = Counter(f["detector"] for f in flags)
    runs_hit: dict[str, set[str]] = defaultdict(set)
    for f in flags:
        runs_hit[f["detector"]].add(f["run_id"])
    for det, n in by_detector.most_common():
        out.append(f"  {det:<28} {n:>4}  in {len(runs_hit[det])} run(s)")

    rule_of = {(f["run_id"], f["finding_id"]): f["rule_id"] for f in findings}
    rule_cost: Counter = Counter()
    for s in steps:
        rule = rule_of.get((s["run_id"], s["finding_id"]))
        if rule and s["cost"]:
            rule_cost[rule] += s["cost"]
    per_rule: dict[str, Counter] = defaultdict(Counter)
    for f in findings:
        per_rule[f["rule_id"]][f["status"]] += 1
    if per_rule:
        width = max(12, *(len(r) for r in per_rule))
        out += [
            "",
            f"{'rule':<{width}}  total  fixed  suggested  escalated  false+  other  fix rate  cost",
        ]
        for rule in sorted(per_rule, key=lambda r: (-sum(per_rule[r].values()), r)):
            c = per_rule[rule]
            total = sum(c.values())
            human = c["escalated"] + c["failed"]
            other = total - c["fixed"] - c["suggested"] - human - c["false_positive"]
            out.append(
                f"{rule:<{width}}  {total:>5}  {c['fixed']:>5}  {c['suggested']:>9}  {human:>9}  "
                f"{c['false_positive']:>6}  {other:>5}  {c['fixed'] / total:>8.0%}  "
                f"${rule_cost[rule]:.4f}"
            )
    return "\n".join(out) + "\n"
