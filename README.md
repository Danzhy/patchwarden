# patchwarden

An agent that fixes static-analysis warnings in Python repos. It decides which fixes are safe to
apply on its own, proves each one with a re-scan and tests, escalates the risky ones with reasons,
and keeps a trace of every decision.

Status: early development (milestone M0: scaffold and spikes). See `NOTES.md`.

## Development

```sh
uv sync
cp .env.example .env   # add OPENROUTER_API_KEY
uv run pytest
uv run ruff check .
```
