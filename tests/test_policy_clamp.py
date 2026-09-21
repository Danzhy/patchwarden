import pytest

from patchwarden.models import Decision, PreClass, PreClassKind
from patchwarden.policy import clamp

D, K = Decision, PreClassKind

# (pre-class, LLM decision) -> (final decision, clamped)
TABLE = [
    (K.always_escalate, D.auto_fix, D.escalate, True),
    (K.always_escalate, D.suggest, D.escalate, True),
    (K.always_escalate, D.escalate, D.escalate, False),
    (K.always_escalate, D.false_positive, D.escalate, True),
    (K.protected_path, D.auto_fix, D.escalate, True),
    (K.protected_path, D.suggest, D.escalate, True),
    (K.protected_path, D.escalate, D.escalate, False),
    (K.protected_path, D.false_positive, D.escalate, True),
    (K.auto_fix_allowed, D.auto_fix, D.auto_fix, False),
    (K.auto_fix_allowed, D.suggest, D.suggest, False),
    (K.auto_fix_allowed, D.escalate, D.escalate, False),
    (K.auto_fix_allowed, D.false_positive, D.suggest, True),
    (K.llm_decides, D.auto_fix, D.suggest, True),
    (K.llm_decides, D.suggest, D.suggest, False),
    (K.llm_decides, D.escalate, D.escalate, False),
    (K.llm_decides, D.false_positive, D.false_positive, False),
]


@pytest.mark.parametrize(("kind", "llm", "decision", "clamped"), TABLE)
def test_clamp_table(kind, llm, decision, clamped):
    r = clamp(llm, PreClass(kind=kind, reason="because"))
    assert (r.decision, r.clamped) == (decision, clamped)
    if clamped:
        assert "because" in r.reason


def test_table_is_complete():
    assert {(k, d) for k, d, _, _ in TABLE} == {(k, d) for k in K for d in D}
