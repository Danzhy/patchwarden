"""LLMClient (retries, JSON repair, budget, trace steps) and the OpenRouter transport, offline."""

import json

import httpx
import pytest
from fake_llm import FakeTransport, fake_client, triage_json

from patchwarden.config import Config
from patchwarden.llm import (
    BudgetExceeded,
    LLMError,
    OpenRouterTransport,
    RawReply,
    TransientLLMError,
)
from patchwarden.models import TriageOutput
from patchwarden.tracing.store import TraceStore

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "Rule: ruff:F841\nhi"}]


@pytest.fixture
def run(tmp_path):
    store = TraceStore(tmp_path / "t")
    r = store.start_run(repo="r", trigger="test", config_hash="h", prompt_version="p", models={})
    yield r
    store.close()


def client(run, script, **kw):
    t = FakeTransport({("triage", "*"): script})
    return fake_client(t, Config(), run, **kw), t


def llm_steps(run):
    return [s for s in run.store.rows("steps", run.run_id) if s["node"].startswith("llm:")]


def test_valid_json(run):
    c, t = client(run, triage_json("suggest"))
    reply = c.complete("triage", MSGS, schema=TriageOutput, finding_id="fp")
    assert reply.parsed.decision == "suggest"
    [step] = llm_steps(run)
    assert step["finding_id"] == "fp" and step["cost"] == pytest.approx(0.001)
    assert step["model"] == Config().models["triage"]
    assert json.loads(step["output_json"])["parsed"]["decision"] == "suggest"


def test_json_in_a_fence_is_accepted(run):
    c, _ = client(run, "```json\n" + triage_json("escalate") + "\n```")
    assert c.complete("triage", MSGS, schema=TriageOutput).parsed.decision == "escalate"


def test_json_repaired_on_second_turn(run):
    c, t = client(run, ['{"decision": "maybe"}', triage_json("auto_fix")])
    reply = c.complete("triage", MSGS, schema=TriageOutput)
    assert reply.parsed.decision == "auto_fix"
    repair = t.calls[1].messages
    assert repair[-2] == {"role": "assistant", "content": '{"decision": "maybe"}'}
    assert "not valid JSON" in repair[-1]["content"] and "decision" in repair[-1]["content"]
    [step] = llm_steps(run)  # one step for the whole exchange, costs summed
    assert step["cost"] == pytest.approx(0.002) and step["error"] is None


def test_invalid_json_twice(run):
    c, _ = client(run, ["nope", "still nope"])
    with pytest.raises(LLMError) as e:
        c.complete("triage", MSGS, schema=TriageOutput)
    assert e.value.kind == "invalid_json_from_llm"
    assert "invalid_json_from_llm" in llm_steps(run)[0]["error"]


def test_truncated_reply(run):
    c, _ = client(run, RawReply(None, "length", "m", tokens_out=800, reasoning_tokens=800))
    with pytest.raises(LLMError) as e:
        c.complete("triage", MSGS, schema=TriageOutput)
    assert e.value.kind == "llm_truncated"
    assert llm_steps(run)[0]["reasoning_tokens"] == 800


def test_truncated_reply_with_partial_content(run):
    c, _ = client(run, RawReply('{"decision": "sugg', "length", "m", tokens_out=800))
    with pytest.raises(LLMError) as e:
        c.complete("triage", MSGS, schema=TriageOutput)
    assert e.value.kind == "llm_truncated"


def test_budget_is_checked_before_the_repair_turn(run):
    c, t = client(run, ["nope", triage_json("auto_fix")], budget_usd=0.001)
    with pytest.raises(BudgetExceeded):
        c.complete("triage", MSGS, schema=TriageOutput)
    assert len(t.calls) == 1


def test_transient_errors_are_retried(run):
    c, t = client(run, [TransientLLMError("429"), TransientLLMError("502"), "plain text"])
    slept = []
    c.sleep = slept.append
    assert c.complete("triage", MSGS).text == "plain text"
    assert slept == [1.0, 2.0] and len(t.calls) == 3
    assert json.loads(llm_steps(run)[0]["output_json"])["attempts"] == 3


def test_transient_errors_exhausted(run):
    c, _ = client(run, [TransientLLMError("429")] * 3)
    with pytest.raises(LLMError, match="3 attempts failed"):
        c.complete("triage", MSGS)


def test_budget_is_checked_before_each_call(run):
    c, t = client(run, ["a", "b"], budget_usd=0.001)
    c.complete("triage", MSGS)
    with pytest.raises(BudgetExceeded):
        c.complete("triage", MSGS)
    assert len(t.calls) == 1
    assert "budget_exceeded" in llm_steps(run)[1]["error"]


def test_key_is_redacted_in_trace(run, monkeypatch):
    key = "sk-or-v1-" + "a" * 40
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    c, _ = client(run, f"echo {key} and Bearer {key}")
    c.complete("triage", [{"role": "user", "content": f"my key is {key}"}])
    raw = run.jsonl.read_text() + json.dumps(run.store.rows("steps", run.run_id))
    assert key not in raw and "[REDACTED]" in raw


def test_make_client_without_key(run):
    from patchwarden.llm import make_client

    with pytest.raises(LLMError) as e:
        make_client(Config(), run)
    assert e.value.kind == "no_api_key"


# --- OpenRouterTransport against a mocked HTTP layer ---

OK_BODY = {
    "id": "gen-1",
    "object": "chat.completion",
    "created": 0,
    "model": "deepseek/deepseek-v4.1-flash",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": '{"decision": "suggest"}'},
        }
    ],
    "usage": {
        "prompt_tokens": 39,
        "completion_tokens": 54,
        "total_tokens": 93,
        "cost": 0.0000765,
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}


def transport(handler):
    return OpenRouterTransport(
        "sk-or-v1-test", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_openrouter_request_and_usage():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    raw = transport(handler).send(
        "triage", "x/model", MSGS, json_mode=True, reasoning=False, max_tokens=800
    )
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-or-v1-test"
    body = seen["body"]
    assert body["model"] == "x/model" and body["max_tokens"] == 800
    assert body["reasoning"] == {"enabled": False}
    assert body["usage"] == {"include": True}
    assert body["response_format"] == {"type": "json_object"}
    assert raw.content == '{"decision": "suggest"}'
    assert (raw.tokens_in, raw.tokens_out, raw.cost) == (39, 54, pytest.approx(0.0000765))


def test_openrouter_plain_text_has_no_response_format():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    transport(handler).send("fixer", "m", MSGS, json_mode=False, reasoning=True, max_tokens=10)
    assert "response_format" not in seen["body"]
    assert seen["body"]["reasoning"] == {"enabled": True}


@pytest.mark.parametrize(
    ("status", "exc"),
    [(429, TransientLLMError), (408, TransientLLMError), (503, TransientLLMError), (400, LLMError)],
)
def test_openrouter_errors(status, exc):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "nope", "code": status}})

    with pytest.raises(exc):
        transport(handler).send("t", "m", MSGS, json_mode=False, reasoning=False, max_tokens=5)


def test_openrouter_no_choices_is_transient():
    def handler(request):
        return httpx.Response(200, json={**OK_BODY, "choices": []})

    with pytest.raises(TransientLLMError):
        transport(handler).send("t", "m", MSGS, json_mode=False, reasoning=False, max_tokens=5)
