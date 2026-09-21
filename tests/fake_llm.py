"""A scripted stand-in for OpenRouter. It replaces only the transport, so LLMClient's retry,
JSON repair, budget and tracing logic runs as in production.

The script maps (role, rule_id) to a reply. The rule id comes from the "Rule: <id>" line every
prompt carries; "*" matches any rule. A reply can be a string, a RawReply, an exception to
raise, or a list of those, used in order (for retries).
"""

import json
import re
from dataclasses import dataclass

from patchwarden.llm import LLMClient, RawReply

_RULE = re.compile(r"^Rule: (\S+)$", re.MULTILINE)


@dataclass
class Call:
    role: str
    rule_id: str | None
    messages: list[dict]
    json_mode: bool
    reasoning: bool
    model: str


class FakeTransport:
    def __init__(self, script: dict, cost: float = 0.001):
        self.script = {k: list(v) if isinstance(v, list) else v for k, v in script.items()}
        self.cost = cost
        self.calls: list[Call] = []

    def send(self, role, model, messages, *, json_mode, reasoning, max_tokens):
        users = [m["content"] for m in messages if m["role"] == "user"]
        m = _RULE.search(users[0]) if users else None
        rule = m.group(1) if m else None
        self.calls.append(Call(role, rule, messages, json_mode, reasoning, model))
        key = (role, rule) if (role, rule) in self.script else (role, "*")
        if key not in self.script:
            raise AssertionError(f"unscripted LLM call: {role} {rule}")
        item = self.script[key]
        if isinstance(item, list):
            if not item:
                raise AssertionError(f"script exhausted for {role} {rule}")
            item = item.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, RawReply):
            return item
        return RawReply(
            content=item,
            finish_reason="stop",
            model=model,
            tokens_in=100,
            tokens_out=20,
            cost=self.cost,
        )

    def roles(self) -> list[tuple[str, str | None]]:
        return [(c.role, c.rule_id) for c in self.calls]


def triage_json(decision: str, reason: str = "scripted", risk_notes: str = "", conf=0.9) -> str:
    return json.dumps(
        {"decision": decision, "confidence": conf, "reason": reason, "risk_notes": risk_notes}
    )


def verifier_json(verdict: str = "pass", risk: str = "low", reason: str = "scripted") -> str:
    return json.dumps({"verdict": verdict, "reason": reason, "behaviour_change_risk": risk})


def fixer_reply(file: str, search: str, replace: str, rationale: str = "scripted fix") -> str:
    return (
        f"RATIONALE: {rationale}\n{file}\n<<<<<<< SEARCH\n{search}=======\n{replace}"
        ">>>>>>> REPLACE\n"
    )


def fake_client(transport: FakeTransport, cfg, run, budget_usd=None) -> LLMClient:
    return LLMClient(transport, cfg, run, budget_usd=budget_usd, sleep=lambda s: None)
