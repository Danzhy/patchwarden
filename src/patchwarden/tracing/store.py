"""SQLite trace store, mirrored to one JSONL file per run.

    <trace_dir>/traces.db
    <trace_dir>/runs/<run_id>.jsonl   one {"table": ..., **row} per line, written as it happens

Every text column passes through `redact` first, so an API key can't end up in a trace even if
it appears in a prompt, a reply or an error message.
"""

import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from patchwarden.models import FindingOutcome, Violation

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, repo TEXT, base TEXT,
    git_sha TEXT, trigger TEXT, config_hash TEXT, prompt_version TEXT, models_json TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, duration_ms INTEGER, outcome TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT, step_id INTEGER, parent_step_id INTEGER, node TEXT, finding_id TEXT,
    started_at TEXT, input_json TEXT, output_json TEXT, tool_calls_json TEXT, model TEXT,
    tokens_in INTEGER, tokens_out INTEGER, reasoning_tokens INTEGER, cost REAL,
    latency_ms INTEGER, error TEXT,
    PRIMARY KEY (run_id, step_id)
);
CREATE TABLE IF NOT EXISTS findings (
    run_id TEXT, finding_id TEXT, rule_id TEXT, file TEXT, line INTEGER, message TEXT,
    pre_class TEXT, triage_decision TEXT, final_decision TEXT, clamped INTEGER,
    fix_rounds INTEGER, status TEXT, fixed_by TEXT, reason TEXT,
    PRIMARY KEY (run_id, finding_id)
);
CREATE TABLE IF NOT EXISTS violations (run_id TEXT, finding_id TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS flags (run_id TEXT, finding_id TEXT, detector TEXT, detail TEXT);
"""

_KEY_PATTERNS = [
    re.compile(r"sk-or-[\w-]{10,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})"),  # GitHub tokens (CI)
    re.compile(r"Bearer\s+\S+"),
]
_SECRET_ENV = ("OPENROUTER_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")
REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    for name in _SECRET_ENV:
        key = os.environ.get(name)
        if key and len(key) >= 8:
            text = text.replace(key, REDACTED)
    for pat in _KEY_PATTERNS:
        text = pat.sub(REDACTED, text)
    return text


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return redact(json.dumps(value, default=str, ensure_ascii=False))


TABLES = ("runs", "steps", "findings", "violations", "flags")


class TraceStore:
    def __init__(self, trace_dir: Path):
        self.dir = trace_dir
        (trace_dir / "runs").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(trace_dir / "traces.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def start_run(
        self,
        *,
        repo: str,
        trigger: str,
        config_hash: str,
        prompt_version: str,
        models: dict[str, str],
        base: str | None = None,
        git_sha: str | None = None,
    ) -> "Run":
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        run = Run(self, f"{stamp}-{secrets.token_hex(3)}")
        run.insert(
            "runs",
            run_id=run.run_id,
            started_at=_now(),
            repo=repo,
            base=base,
            git_sha=git_sha,
            trigger=trigger,
            config_hash=config_hash,
            prompt_version=prompt_version,
            models_json=_json(models),
        )
        return run

    def rows(self, table: str, run_id: str | None = None) -> list[dict]:
        """A table's rows, for one run or (run_id None) all of them."""
        if run_id is None:
            return [dict(r) for r in self.db.execute(f"SELECT * FROM {table}")]
        cur = self.db.execute(f"SELECT * FROM {table} WHERE run_id = ?", (run_id,))
        return [dict(r) for r in cur]

    def close(self) -> None:
        self.db.close()


class Step:
    """Filled in by the caller inside `Run.step(...)`; written when the block exits."""

    def __init__(self, step_id: int):
        self.step_id = step_id
        self.output: Any = None
        self.model: str | None = None
        self.tokens_in = 0
        self.tokens_out = 0
        self.reasoning_tokens = 0
        self.cost = 0.0
        self.error: str | None = None


class Run:
    def __init__(self, store: TraceStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self.jsonl = store.dir / "runs" / f"{run_id}.jsonl"
        self._next_step = 1
        self._t0 = time.monotonic()

    def insert(self, table: str, **row: Any) -> None:
        row = {k: redact(v) if isinstance(v, str) else v for k, v in row.items()}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.store.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
        self.store.db.commit()
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"table": table, **row}, ensure_ascii=False) + "\n")

    @contextmanager
    def step(
        self,
        node: str,
        *,
        finding_id: str | None = None,
        parent: int | None = None,
        input: Any = None,
    ):
        step = Step(self._next_step)
        self._next_step += 1
        started = _now()
        t0 = time.monotonic()
        try:
            yield step
        except BaseException as e:
            step.error = step.error or f"{type(e).__name__}: {e}"
            raise
        finally:
            self.insert(
                "steps",
                run_id=self.run_id,
                step_id=step.step_id,
                parent_step_id=parent,
                node=node,
                finding_id=finding_id,
                started_at=started,
                input_json=_json(input),
                output_json=_json(step.output),
                tool_calls_json=None,
                model=step.model,
                tokens_in=step.tokens_in,
                tokens_out=step.tokens_out,
                reasoning_tokens=step.reasoning_tokens,
                cost=step.cost,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=step.error,
            )

    def finding(self, o: FindingOutcome) -> None:
        self.insert(
            "findings",
            run_id=self.run_id,
            finding_id=o.finding.fingerprint,
            rule_id=o.finding.rule_id,
            file=o.finding.file,
            line=o.finding.region.start_line,
            message=o.finding.message,
            pre_class=o.preclass.kind,
            triage_decision=o.triage.decision if o.triage else None,
            final_decision=o.clamp.decision if o.clamp else None,
            clamped=int(o.clamp.clamped) if o.clamp else None,
            fix_rounds=o.fix_rounds,
            status=o.status,
            fixed_by=o.fixed_by,
            reason=o.reason,
        )

    def violation(self, finding_id: str, v: Violation) -> None:
        self.insert(
            "violations",
            run_id=self.run_id,
            finding_id=finding_id,
            kind=v.kind,
            detail=f"{v.file}: {v.detail}",
        )

    def flag(self, finding_id: str | None, detector: str, detail: str) -> None:
        self.insert(
            "flags", run_id=self.run_id, finding_id=finding_id, detector=detector, detail=detail
        )

    def finish(self, outcome: str) -> dict:
        tin, tout, cost = self.store.db.execute(
            "SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), "
            "COALESCE(SUM(cost), 0) FROM steps WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        totals = {
            "finished_at": _now(),
            "tokens_in": tin,
            "tokens_out": tout,
            "cost_usd": cost,
            "duration_ms": int((time.monotonic() - self._t0) * 1000),
            "outcome": outcome,
        }
        sets = ", ".join(f"{k} = ?" for k in totals)
        self.store.db.execute(
            f"UPDATE runs SET {sets} WHERE run_id = ?", [*totals.values(), self.run_id]
        )
        self.store.db.commit()
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"table": "runs_update", "run_id": self.run_id, **totals}) + "\n")
        return totals
