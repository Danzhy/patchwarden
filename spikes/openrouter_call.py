"""M0 spike: one OpenRouter call; check JSON-mode output and the usage/cost fields.

Usage: uv run python spikes/openrouter_call.py [model_id]
Needs OPENROUTER_API_KEY in the environment or .env.
"""

import json
import os
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
model = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4.1-flash"
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])


def call(reasoning: dict | None) -> None:
    extra: dict = {"usage": {"include": True}}
    if reasoning is not None:
        extra["reasoning"] = reasoning
    t0 = time.monotonic()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": 'Reply with JSON only: {"decision": "...", "reason": "..."}',
            },
            {"role": "user", "content": "Ruff F401: `import os` is unused. auto_fix or escalate?"},
        ],
        response_format={"type": "json_object"},
        max_tokens=1000,
        extra_body=extra,
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    choice = resp.choices[0]
    msg = choice.message
    reasoning_text = getattr(msg, "reasoning", None) or ""

    print(f"== reasoning={reasoning} | model: {resp.model} | latency_ms: {latency_ms}")
    print("finish_reason:", choice.finish_reason, "| reasoning chars:", len(reasoning_text))
    print("content:", msg.content)
    if msg.content:
        try:
            print("parsed:", json.loads(msg.content))
        except json.JSONDecodeError as e:
            print("invalid JSON:", e)
    print("usage:", resp.usage.model_dump() if resp.usage else None)


# Default provider behaviour, then with reasoning switched off (OpenRouter's unified param).
call(None)
call({"enabled": False})
