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

t0 = time.monotonic()
resp = client.chat.completions.create(
    model=model,
    messages=[
        {"role": "system", "content": 'Reply with JSON only: {"decision": "...", "reason": "..."}'},
        {"role": "user", "content": "Ruff F401: `import os` is unused. auto_fix or escalate?"},
    ],
    response_format={"type": "json_object"},
    max_tokens=200,
    extra_body={"usage": {"include": True}},
)
latency_ms = int((time.monotonic() - t0) * 1000)

print("model:", resp.model, "| latency_ms:", latency_ms)
print("content:", resp.choices[0].message.content)
print("parsed:", json.loads(resp.choices[0].message.content))
print("usage:", resp.usage.model_dump() if resp.usage else None)
