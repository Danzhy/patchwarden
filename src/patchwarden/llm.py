"""Every LLM call goes through here: OpenRouter transport, retries, JSON validation, budget,
and one trace step per call (tokens, cost, latency, error).

Tests swap the transport for tests/fake_llm.FakeTransport; nothing else changes, so the retry,
repair and budget logic below is what the tests exercise.
"""

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError

from patchwarden.config import Config
from patchwarden.tracing.store import Run

OPENROUTER_URL = "https://openrouter.ai/api/v1"
MAX_TOKENS = {"triage": 800, "fixer": 4000, "verifier": 1000}
ATTEMPTS = 3  # per request, on transient errors
BACKOFF_S = [1.0, 2.0]


class LLMError(RuntimeError):
    """kind: api_error | llm_truncated | invalid_json_from_llm | budget_exceeded | no_api_key."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


class BudgetExceeded(LLMError):
    def __init__(self, spent: float, budget: float):
        super().__init__("budget_exceeded", f"spent ${spent:.4f} of ${budget:.2f}")


class TransientLLMError(RuntimeError):
    """Rate limit, timeout, connection or 5xx: worth retrying."""


@dataclass
class RawReply:
    content: str | None
    finish_reason: str | None
    model: str | None
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0


class Transport(Protocol):
    def send(
        self,
        role: str,
        model: str,
        messages: list[dict],
        *,
        json_mode: bool,
        reasoning: bool,
        max_tokens: int,
    ) -> RawReply: ...


class OpenRouterTransport:
    def __init__(self, api_key: str, timeout: float = 60, http_client=None):
        from openai import OpenAI

        # max_retries=0: retries happen in LLMClient, so each one is counted in the trace.
        self.client = OpenAI(
            base_url=OPENROUTER_URL,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    def send(self, role, model, messages, *, json_mode, reasoning, max_tokens) -> RawReply:
        import openai

        kwargs: dict = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0,
                # usage.include: OpenRouter reports the cost itself, so no price table here.
                extra_body={"usage": {"include": True}, "reasoning": {"enabled": reasoning}},
                **kwargs,
            )
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as e:
            raise TransientLLMError(str(e)) from e
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientLLMError(str(e)) from e
            raise LLMError("api_error", f"HTTP {e.status_code}: {e.message}") from e
        if not resp.choices:  # OpenRouter sometimes returns 200 with an error body
            raise TransientLLMError(f"no choices in response: {resp.model_dump_json()[:300]}")
        choice = resp.choices[0]
        usage = resp.usage.model_dump() if resp.usage else {}
        details = usage.get("completion_tokens_details") or {}
        return RawReply(
            content=choice.message.content,
            finish_reason=choice.finish_reason,
            model=resp.model,
            tokens_in=usage.get("prompt_tokens") or 0,
            tokens_out=usage.get("completion_tokens") or 0,
            reasoning_tokens=details.get("reasoning_tokens") or 0,
            cost=float(usage.get("cost") or 0.0),
        )


@dataclass
class LLMReply:
    text: str
    parsed: BaseModel | None
    cost: float


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json(text: str, schema: type[BaseModel]) -> BaseModel:
    m = _FENCE.match(text)
    return schema.model_validate_json(m.group(1) if m else text)


class LLMClient:
    def __init__(
        self,
        transport: Transport,
        cfg: Config,
        run: Run,
        *,
        budget_usd: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.transport = transport
        self.cfg = cfg
        self.run = run
        self.budget = cfg.budget_usd if budget_usd is None else budget_usd
        self.spent = 0.0
        self.sleep = sleep

    def complete(
        self,
        role: str,
        messages: list[dict],
        *,
        schema: type[BaseModel] | None = None,
        finding_id: str | None = None,
        parent: int | None = None,
    ) -> LLMReply:
        model = self.cfg.models[role]
        with self.run.step(
            f"llm:{role}",
            finding_id=finding_id,
            parent=parent,
            input={"model": model, "messages": messages},
        ) as step:
            step.model = model
            if self.spent >= self.budget:
                raise BudgetExceeded(self.spent, self.budget)
            attempts = 0
            convo = list(messages)
            replies: list[str] = []
            for turn in range(2 if schema else 1):  # schema: one repair turn
                raw, n = self._send(role, model, convo, json_mode=schema is not None)
                attempts += n
                step.model = raw.model or model
                step.tokens_in += raw.tokens_in
                step.tokens_out += raw.tokens_out
                step.reasoning_tokens += raw.reasoning_tokens
                step.cost += raw.cost
                self.spent += raw.cost
                step.output = {"replies": replies, "attempts": attempts}
                if not raw.content:
                    raise LLMError(
                        "llm_truncated" if raw.finish_reason == "length" else "api_error",
                        f"empty reply (finish_reason={raw.finish_reason})",
                    )
                replies.append(raw.content)
                if schema is None:
                    return LLMReply(raw.content, None, step.cost)
                try:
                    parsed = parse_json(raw.content, schema)
                except ValidationError as e:
                    err = _short(e)
                    if turn == 1:
                        raise LLMError("invalid_json_from_llm", err) from e
                    convo += [
                        {"role": "assistant", "content": raw.content},
                        {
                            "role": "user",
                            "content": f"Your reply was not valid JSON for the required "
                            f"schema: {err}\nReply again with only the JSON object.",
                        },
                    ]
                    continue
                step.output["parsed"] = parsed.model_dump(mode="json")
                return LLMReply(raw.content, parsed, step.cost)
        raise AssertionError("unreachable")  # pragma: no cover

    def _send(self, role, model, messages, *, json_mode) -> tuple[RawReply, int]:
        for attempt in range(ATTEMPTS):
            try:
                raw = self.transport.send(
                    role,
                    model,
                    messages,
                    json_mode=json_mode,
                    reasoning=self.cfg.reasoning.get(role, False),
                    max_tokens=MAX_TOKENS.get(role, 1000),
                )
                return raw, attempt + 1
            except TransientLLMError as e:
                if attempt == ATTEMPTS - 1:
                    raise LLMError("api_error", f"{ATTEMPTS} attempts failed: {e}") from e
                self.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        raise AssertionError("unreachable")  # pragma: no cover


def _short(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in e.errors()
    )[:300]


def make_client(cfg: Config, run: Run, budget_usd: float | None = None) -> LLMClient:
    """The real client. Reads OPENROUTER_API_KEY from the environment or a .env in the cwd."""
    from dotenv import load_dotenv

    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise LLMError("no_api_key", "set OPENROUTER_API_KEY (or .env), or use --no-llm")
    return LLMClient(OpenRouterTransport(key), cfg, run, budget_usd=budget_usd)
