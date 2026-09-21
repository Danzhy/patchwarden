import pytest

from patchwarden.config import Config, from_dict
from patchwarden.models import Finding, PreClassKind, Region
from patchwarden.policy import match_rule, pre_classify

K = PreClassKind


def finding(rule_id: str, file: str = "app/mod.py") -> Finding:
    return Finding(
        tool=rule_id.split(":")[0],
        rule_id=rule_id,
        message="m",
        file=file,
        region=Region(start_line=1, end_line=1),
        snippet="x",
        fingerprint="fp",
    )


@pytest.mark.parametrize(
    ("rule_id", "file", "kind"),
    [
        ("bandit:B602", "app/runner.py", K.always_escalate),
        ("ruff:S603", "app/runner.py", K.always_escalate),  # ruff's flake8-bandit rules
        ("ruff:SIM103", "app/shapes.py", K.llm_decides),  # must not match "ruff:S[0-9]*"
        ("codeql:py/sql-injection", "app/db.py", K.always_escalate),
        ("codeql:py/flask-debug", "app/web.py", K.always_escalate),
        # Security wins over the protected path; both escalate, the reason differs.
        ("bandit:B324", "app/auth/tokens.py", K.always_escalate),
        ("ruff:F401", "app/auth/tokens.py", K.protected_path),
        ("ruff:F401", "auth/tokens.py", K.protected_path),
        ("ruff:F401", ".github/scripts/x.py", K.protected_path),
        ("ruff:F401", "app/utils.py", K.auto_fix_allowed),
        ("ruff:UP006", "app/utils.py", K.auto_fix_allowed),
        ("codeql:py/unused-import", "app/utils.py", K.auto_fix_allowed),
        # Allowlisted rule in a test file: not auto-fixed.
        ("ruff:F401", "tests/test_app.py", K.llm_decides),
        ("ruff:F401", "pkg/conftest.py", K.llm_decides),
        ("ruff:B006", "app/utils.py", K.llm_decides),
        ("codeql:py/catch-base-exception", "app/x.py", K.llm_decides),
    ],
)
def test_pre_classify_table(rule_id, file, kind):
    assert pre_classify(finding(rule_id, file), Config()).kind == kind


def test_reason_and_pattern_recorded():
    pc = pre_classify(finding("bandit:B602"), Config())
    assert pc.matched_pattern == "bandit:*"
    assert "bandit:B602" in pc.reason
    assert pre_classify(finding("ruff:B006"), Config()).matched_pattern is None


def test_config_overrides_policy():
    cfg = from_dict({"auto_fix": ["ruff:B006"], "always_escalate": [], "protected_paths": []})
    assert pre_classify(finding("ruff:B006"), cfg).kind == K.auto_fix_allowed
    assert pre_classify(finding("bandit:B602"), cfg).kind == K.llm_decides
    assert pre_classify(finding("ruff:F401", "app/auth/x.py"), cfg).kind == K.llm_decides


def test_match_rule_first_match_wins():
    assert match_rule("ruff:UP006", ["ruff:F*", "ruff:UP*", "ruff:*"]) == "ruff:UP*"
    assert match_rule("ruff:UP006", ["bandit:*"]) is None
