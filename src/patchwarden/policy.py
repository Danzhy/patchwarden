"""Policy in code: pre_classify, clamp, check_diff.

These are pure functions. The LLM can make a decision more cautious, never less; the floor it
can't go below comes from pre_classify. check_diff rejects fixes that game the checks.
"""

import ast
import difflib
import io
import re
import tokenize
from collections import Counter
from collections.abc import Collection
from fnmatch import fnmatchcase

from patchwarden.config import Config
from patchwarden.models import (
    ClampResult,
    Decision,
    Finding,
    PreClass,
    PreClassKind,
    Violation,
    ViolationKind,
)
from patchwarden.scope import is_test_path, match_path
from patchwarden.workspace import split_lines


def match_rule(rule_id: str, patterns: list[str]) -> str | None:
    """The first glob matching a namespaced rule id ("bandit:B602"), else None."""
    return next((p for p in patterns if fnmatchcase(rule_id, p)), None)


def pre_classify(finding: Finding, cfg: Config) -> PreClass:
    """First match wins: always_escalate -> protected_paths -> auto_fix allowlist -> LLM."""
    if pat := match_rule(finding.rule_id, cfg.always_escalate):
        return PreClass(
            kind=PreClassKind.always_escalate,
            reason=f"rule {finding.rule_id} is always escalated",
            matched_pattern=pat,
        )
    if pat := match_path(finding.file, cfg.protected_paths):
        return PreClass(
            kind=PreClassKind.protected_path,
            reason=f"{finding.file} is a protected path",
            matched_pattern=pat,
        )
    if pat := match_rule(finding.rule_id, cfg.auto_fix):
        if is_test_path(finding.file, cfg):
            # Fixes may never touch tests without review (check_diff flags it too, in M2).
            return PreClass(
                kind=PreClassKind.llm_decides,
                reason=f"{finding.rule_id} is allowlisted, but {finding.file} is a test file",
            )
        return PreClass(
            kind=PreClassKind.auto_fix_allowed,
            reason=f"rule {finding.rule_id} is on the auto-fix allowlist",
            matched_pattern=pat,
        )
    return PreClass(
        kind=PreClassKind.llm_decides,
        reason="not on the auto-fix allowlist, so a human reviews any fix",
    )


_CAUTION = {Decision.auto_fix: 0, Decision.suggest: 1, Decision.escalate: 2}
_FLOOR = {
    PreClassKind.always_escalate: Decision.escalate,
    PreClassKind.protected_path: Decision.escalate,
    PreClassKind.auto_fix_allowed: Decision.auto_fix,
    PreClassKind.llm_decides: Decision.suggest,  # only allowlisted rules are ever auto-fixed
}


def clamp(llm: Decision, pre: PreClass) -> ClampResult:
    """The LLM's decision, raised to the policy floor. false_positive (dismiss the finding) is
    accepted only where the LLM decides; elsewhere it becomes the floor, or suggest."""
    floor = _FLOOR[pre.kind]
    if llm == Decision.false_positive:
        if pre.kind == PreClassKind.llm_decides:
            return ClampResult(decision=llm, clamped=False, reason="LLM: false positive")
        decision = max(floor, Decision.suggest, key=_CAUTION.__getitem__)
        return ClampResult(
            decision=decision,
            clamped=True,
            reason=f"false_positive not allowed for {pre.kind}; {pre.reason}",
        )
    if _CAUTION[llm] >= _CAUTION[floor]:
        return ClampResult(decision=llm, clamped=False, reason=f"LLM: {llm}")
    return ClampResult(decision=floor, clamped=True, reason=f"raised to {floor}: {pre.reason}")


# Comment markers that silence an analyzer instead of fixing the code.
SUPPRESSIONS = {
    "noqa": re.compile(r"#\s*noqa\b", re.I),
    "type: ignore": re.compile(r"#\s*type:\s*ignore\b"),
    "nosec": re.compile(r"#\s*nosec\b", re.I),
    "pragma: no cover": re.compile(r"#\s*pragma:\s*no\s*cover\b"),
    "pylint: disable": re.compile(r"#\s*pylint:\s*disable\b"),
    "lgtm": re.compile(r"#\s*lgtm\b", re.I),
    "codeql": re.compile(r"#\s*codeql\[", re.I),
}


def check_diff(
    changes: dict[str, tuple[str, str]],
    cfg: Config,
    *,
    target_file: str,
    rule_ids: Collection[str],
    max_lines: int | None,
) -> list[Violation]:
    """Every way the change `{file: (before, after)}` breaks policy. Empty means it's clean."""
    out: list[Violation] = []
    V, K = Violation, ViolationKind
    allow_defaults = any(match_rule(r, cfg.signature_rules) for r in rule_ids)
    total = 0
    for file, (before, after) in changes.items():
        if is_test_path(file, cfg):
            out.append(V(kind=K.test_file_touched, file=file, detail="fixes may not edit tests"))
        if pat := match_path(file, cfg.protected_paths):
            out.append(V(kind=K.protected_file_touched, file=file, detail=f"matches {pat}"))
        if file != target_file:
            out.append(
                V(kind=K.other_file_touched, file=file, detail=f"the finding is in {target_file}")
            )
        total += changed_line_count(before, after)
        out.extend(_suppressions(file, before, after))
        try:
            new_tree = ast.parse(after)
        except SyntaxError as e:
            out.append(V(kind=K.syntax_error, file=file, detail=f"line {e.lineno}: {e.msg}"))
            continue
        try:
            old_tree = ast.parse(before)
        except SyntaxError:
            continue  # nothing to compare against
        out.extend(_definitions(file, old_tree, new_tree, allow_defaults))
    if max_lines is not None and total > max_lines:
        out.append(
            V(
                kind=K.too_many_lines,
                file=target_file,
                detail=f"{total} lines changed, limit {max_lines}",
            )
        )
    return out


def changed_line_count(before: str, after: str) -> int:
    """Removed plus added lines. From the opcodes, not diff text, where a removed line that
    itself starts with "--" (an rst underline) would look like a "---" header."""
    sm = difflib.SequenceMatcher(None, split_lines(before), split_lines(after), autojunk=False)
    return sum(i2 - i1 + j2 - j1 for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")


def _comments(text: str) -> list[str]:
    try:
        return [
            t.string
            for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type == tokenize.COMMENT
        ]
    except (tokenize.TokenError, SyntaxError):
        # Unparseable: fall back to anything after a "#" (over-counts strings, which only
        # makes the check stricter).
        return [ln[ln.index("#") :] for ln in text.splitlines() if "#" in ln]


def _suppressions(file: str, before: str, after: str) -> list[Violation]:
    def count(text: str) -> Counter:
        c: Counter = Counter()
        for comment in _comments(text):
            for name, pat in SUPPRESSIONS.items():
                c[name] += len(pat.findall(comment))
        return c

    old, new = count(before), count(after)
    return [
        Violation(
            kind=ViolationKind.suppression_added,
            file=file,
            detail=f"adds a '# {name}' comment",
        )
        for name in SUPPRESSIONS
        if new[name] > old[name]
    ]


def _defs(tree: ast.AST) -> dict[str, ast.AST]:
    """Qualified name -> node for every def/class, e.g. "Square.area"."""
    out: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = prefix + child.name
                out.setdefault(name, child)
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, str]]:
    a = fn.args
    params = [(p.arg, "posonly") for p in a.posonlyargs] + [(p.arg, "arg") for p in a.args]
    if a.vararg:
        params.append((a.vararg.arg, "*"))
    params += [(p.arg, "kwonly") for p in a.kwonlyargs]
    if a.kwarg:
        params.append((a.kwarg.arg, "**"))
    return params


def _defaults(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    return [ast.dump(d) for d in a.defaults] + [
        ast.dump(d) if d is not None else "-" for d in a.kw_defaults
    ]


def _definitions(
    file: str, old_tree: ast.AST, new_tree: ast.AST, allow_defaults: bool
) -> list[Violation]:
    old, new = _defs(old_tree), _defs(new_tree)
    out = [
        Violation(kind=ViolationKind.definition_removed, file=file, detail=f"removes {name}")
        for name in old
        if name not in new
    ]
    for name, node in old.items():
        new_node = new.get(name)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or not isinstance(
            new_node, ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        if _params(node) != _params(new_node):
            detail = f"changes the parameters of {name}"
        elif _defaults(node) != _defaults(new_node) and not allow_defaults:
            detail = f"changes a default value in {name}"
        else:
            continue
        out.append(Violation(kind=ViolationKind.signature_changed, file=file, detail=detail))
    return out
