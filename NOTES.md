# Development notes

## M0: scaffold and spikes (2026-09-21)

### Environment
- Python 3.12 via uv (`.python-version`); system Python is 3.14 and isn't used.
- Tools: ruff 0.x and bandit[sarif] as project dependencies. CodeQL CLI 2.25.1 at
  `~/codeql-home/codeql/codeql`, queries from a source checkout at `~/codeql-home/codeql-repo`
  (commit `fb8b5699f28`, 2026-04-02).
- The CodeQL JVM writes to the system temp dir and ignores `TMPDIR`, so it can't run inside the
  Claude Code sandbox. Run it unsandboxed. The same applies to `uv` (it needs `~/.cache/uv`), so
  sandboxed commands call `.venv/bin/...` directly.

### Fixture repo (`tests/fixtures/repo_small/`)
Five passing tests (`python -m pytest -q`). With `--select E,F,B,UP,SIM` ruff reports 12 findings:
F401 ×3, F841, E711, B006, SIM103, UP004, UP006, UP032, UP035. Bandit reports 3: B324 (md5, in
`app/auth/`, a protected path), B404, B602 (`shell=True`). `app/clean.py` has no findings.

### SARIF shape (`spikes/sarif_shape.py`, captured in `tests/fixtures/sarif/`)
| | ruff | bandit |
|---|---|---|
| SARIF version | 2.1.0 | 2.1.0 |
| `artifactLocation.uri` | absolute `file:///...` | relative to cwd (`app/runner.py`) |
| `region.snippet` | absent | present (plus `contextRegion`) |
| `partialFingerprints` | absent | absent |
| severity | `level` only (`error`) | `level` + `properties.issue_severity/confidence` |
| extra | `fixes` (ruff's own fix) | `ruleIndex` |
| exit code with findings | 1 | 1 |

Consequences for M1:
- Normalise URIs: strip `file://`, make them relative to the repo root.
- Read the snippet from the file when SARIF has none. Neither tool gives partialFingerprints, so
  the fallback hash (tool, rule, file, whitespace-normalised snippet) is the normal path.
- Exit code 1 means "findings", not failure; treat only invalid JSON or other codes as errors.
- The stored fixture SARIF has repo-relative URIs so it doesn't embed this machine's paths.

### CodeQL (`spikes/codeql_reproduce.py`)
- `database create --language=python` takes 3–8 s, even for 240 files.
- The first `database analyze` with the `python-code-scanning.qls` suite took 435 s because it
  compiles queries from source. After that the suite runs in about 18 s and the 12 eval queries
  in about 17 s. So re-scanning is feasible if it's batched: one DB over all the fixed files per
  condition and round, not one DB per file.
- The security suite finds nothing in the fixture repo (no taint source). That's expected. CodeQL
  is only used for eval A.

### CodeQueries (benchmark A)
- HF `thepurpleowl/codequeries`, Apache-2.0. Used the `file_ideal` test split (JSONL, 1.1 GB,
  44,421 rows, 52 queries, 16,711 positive rows). It's stored in `eval/data/` (gitignored).
- **Pitfall:** rows don't contain whole files. `context_blocks` holds only the blocks relevant
  to the query, and each block has blank placeholder lines where other functions and classes go.
  Joining the blocks isn't valid Python in about 74% of rows, and rebuilding from block offsets
  is lossy. Running CodeQL on block content gives wrong results (for example, "unused import"
  fires because the code that uses the import is missing).
- **Fix:** CodeQueries files come from ETH Py150 Open, so the whole files are taken from the
  Py150 archive (`files.sri.inf.ethz.ch/data/py150_files.tar.gz`, 199 MB, extracted to
  `eval/data/py150/data/<owner>/<repo>/<path>`). Py150 Open is the licence-vetted subset
  (MIT/BSD/Apache/GPL...), and CodeQueries uses only files from it.
- `answer_spans[].start_line` is **0-based**. The manifest stores 1-based lines, matching SARIF.
- A lot of the corpus is Python 2. The subset keeps only files that parse as Python 3, have at
  most 400 lines, and whose labelled span text is found at the labelled line.
- Subset (`spikes/codequeries_subset.py select` → `eval/subsets/codequeries.json`): 12 rules ×
  20 files = 240 items. Seed 20260921. Dev/test is split **by source repo** (30% of repos go to
  dev), giving 70 dev and 170 test items. Each item records the file's sha256.
- Reproduction check: current CodeQL reports the labelled finding for **233/240** items. The
  manifest marks each item with `reproduces`, and the eval scores only those. Two rules were
  dropped because they rarely reproduce, probably because they need the imported module to
  resolve: "Module is imported with 'import' and 'import from'" (6/20) and "Module is imported
  more than once" (2/20).
- Rules: unused import, unused local variable, except BaseException, variable defined multiple
  times, unreachable code, testing equality to None, unnecessary pass, import of deprecated
  module, `import *`, redundant assignment, unnecessary `else` in loop, and Flask debug mode
  (security, so policy should escalate it).

### A2 repos (`eval/subsets/real_repos.json`)
more-itertools, toolz, boltons, humanize, cachetools, python-tabulate, marshmallow, tenacity, all
pinned by SHA on 2026-09-21. They're all pure Python with MIT, Apache or BSD-style licences.
Test runtime and finding counts with the default rules still need checking at the start of M6;
drop any repo whose suite takes more than about 60 s.

### Experiment B candidate
EvalPlus MBPP+ (tasks with strong tests). Deferred, since B is optional.

### OpenRouter (`spikes/openrouter_call.py`)
- **Default model candidates** (price per M tokens, input/output; defaults only, always
  overridable in config):
  - triage (cheap): `deepseek/deepseek-v4.1-flash` ($0.15 / $0.60)
  - fixer and verifier (strong): `anthropic/claude-sonnet-5` ($2 / $10)
- **Pitfall: reasoning is on by default.** With `max_tokens=200`, and again with 1000,
  deepseek-v4.1-flash spent every output token reasoning: `finish_reason="length"`,
  `reasoning_tokens=1000`, `content=None`. That call cost $0.0009 and returned no answer.
- With `extra_body={"reasoning": {"enabled": False}}`, the same prompt returned valid JSON with
  `finish_reason="stop"` in 1.4 s, using 39 prompt and 54 completion tokens, for $0.0000765.
- `response_format={"type": "json_object"}` is honoured, and the output parsed first time.
- **Cost is reported directly** when `extra_body={"usage": {"include": True}}` is sent:
  `usage.cost` (USD), plus `cost_details` and `completion_tokens_details.reasoning_tokens`. So
  there's no need to keep a price table in the code.
- Sandbox: Python's TLS through the Claude Code sandbox proxy fails with `OSStatus -26276`
  (certificate trust), so real LLM calls run unsandboxed. This doesn't affect tests, which make
  no network calls.

Consequences for `llm.py` (M3):
- A `reasoning` setting per role in config. Default: off for triage; the fixer and verifier
  defaults get decided when M3 is tested against real models.
- Treat `finish_reason == "length"` with empty content as its own error
  (`llm_truncated`, logged as a `step_error`), separate from `invalid_json_from_llm`.
- Take cost from `usage.cost`, and record `reasoning_tokens` in the step's trace row.

## M1: `scan` (2026-09-21)
- Pipeline (`scan.py`): scope → analyzers → SARIF → `Finding` → `policy.pre_classify` → report.
  `patchwarden scan tests/fixtures/repo_small` finds 15: escalate 3 (bandit), protected 1 (F401 in
  `app/auth/`), auto-fix 8, triage 3 (SIM103 ×2, B006). Scan never writes to the repo (tested).
- Rule ids are namespaced (`ruff:F401`, `bandit:B602`, `codeql:py/unused-import`) and config
  patterns are `fnmatch` globs over them. **Pitfall found on the first run:** `ruff:S*` (meant for
  ruff's flake8-bandit S-rules) also matches `ruff:SIM103`, so a simplification got escalated as
  security. The default is now `ruff:S[0-9]*`, with a regression test.
- Path globs: fnmatch's `*` crosses `/`; each path is also matched as `"/" + path`, so
  `**/auth/**` covers a top-level `auth/` too, while `app/oauth/` doesn't match.
- Fingerprint = sha256(tool, rule, file, CodeQL line hash or whitespace-normalised snippet), 16
  hex chars. No line numbers, so it survives code moving above it. Identical rule+snippet in one
  file: the 2nd, 3rd... by line get `:2`, `:3`; the first keeps the bare hash.
- Ruff runs with `--isolated` and patchwarden's `ruff_select`, so the target repo's ruff config
  doesn't change what's reported (keeps evals reproducible). Bandit skips `test_paths` (B101 fires
  on every `assert`). Tools run as `sys.executable -m ruff|bandit`, i.e. the venv's versions.
- Config: unknown `[tool.patchwarden]` keys and wrong types (including `true` for an int) are
  errors, exit code 2. Keys used by later milestones (`test_command`, `max_fix_rounds`,
  `budget_usd`, `models`...) are already in the schema.
- CodeQL: off by default (`analyzers = ["ruff", "bandit", "codeql"]` to enable; `codeql_queries`
  to choose queries). Checked for real on the fixture: the code-scanning suite finds nothing (as in
  M0) and `UnusedImport.ql` finds the 3 unused imports with correct paths/snippets, ~23 s. Tests
  use a fake `codeql` script.
- 61 tests, 96% line coverage overall, 100% on `policy.py` and `config.py`.

## M2: workspace, deterministic pass, edits, clamp, check_diff (2026-09-22)
- **Pitfall found while planning:** `ruff check --fix` over the repo also deletes the unused
  import in `app/auth/tokens.py`, a protected path. So the deterministic pass runs ruff per file,
  `--select` limited to that file's `auto_fix_allowed` rules, `--fix-only`, never
  `--unsafe-fixes`. On the fixture it resolves 6 of the 8 auto-fix candidates (F401 ×2, UP035,
  UP006 in utils.py; UP004, UP032 in shapes.py). F841 and E711 only have unsafe fixes and stay
  for the Fixer. Each fixed file is re-scanned with all analyzers and reverted if a new
  fingerprint appears, check_diff complains, or none of its selected findings went away.
- Workspace = plain temp copy (skipping `.git`, `.venv`...), not a git worktree: a worktree starts
  from a commit and would drop uncommitted work. Changes are detected on disk against a snapshot,
  so ruff's in-place edits count. `apply_to_source` (only `--apply`) refuses, writing nothing, if
  a target file changed in the repo since the copy. Reads/writes keep CRLF; lines split on `\n`
  only; the diff carries `\ No newline at end of file`; tested with `git apply --check`.
- Edits: SEARCH/REPLACE blocks, path on the line above (a ```python fence is skipped). Exact
  unique match, one fallback ignoring trailing whitespace, still unique. Errors: `bad_path`
  (absolute, `..`, not an existing UTF-8 `.py`), `empty_search`, `no_match`, `ambiguous`. All or
  nothing across blocks.
- `clamp`: escalate for always_escalate / protected_path whatever the LLM says; floor auto_fix
  for the allowlist, suggest otherwise, so only allowlisted rules are ever auto-fixed.
  `false_positive` is accepted only when pre-class is `llm_decides` (else → suggest/escalate).
  `clamped=True` feeds the `triage_policy_disagreement` trace in M3.
- `check_diff` violations: `test_file_touched`, `protected_file_touched`, `other_file_touched`,
  `too_many_lines`, `syntax_error`, `suppression_added` (counted in real comments via `tokenize`,
  so `"# noqa"` in a string is fine; noqa, type: ignore, nosec, pragma: no cover, pylint:
  disable, lgtm, codeql[...]), `definition_removed` (qualified names like `Square.area`),
  `signature_changed` (parameter names/kinds/order always; defaults unless the rule is in the new
  `signature_rules` config key: B006, B008, codeql modification-of-default-value; annotations
  may change, UP006 rewrites them).
- `patchwarden fix REPO [--base] [--output patchwarden.patch] [--apply]` runs the deterministic
  pass only for now; M3 adds the LLM stages and `--no-llm` to keep this behaviour.
- 133 tests, 96% coverage overall, 98% on `policy.py`.

## M3: LLM client, Triage + Fixer graph, trace store (2026-09-22)
- `fix` is now the full pipeline: scan → ruff's safe fixes → per finding: Triage → `clamp` →
  Fixer → apply edits → `check_diff` → markdown report. Output: `patchwarden.patch`,
  `patchwarden-report.md`, and a trace in `.patchwarden/` (SQLite + one JSONL file per run), all
  in the cwd by default. Exit 1 if anything is escalated or couldn't be fixed. `--no-llm` keeps
  the M2 behaviour; `--budget-usd` overrides `budget_usd`.
- **One LangGraph graph per finding** (`triage → clamp → fixer → apply ⟲ one retry →
  finalize`), invoked in a plain loop. State is small and serialisable; the workspace, LLM,
  trace run and config are closure dependencies. This keeps each finding's trace self-contained
  and stays clear of LangGraph's 25-step recursion limit on big runs.
- **Bottom-up order** within a file: an edit only shifts lines below it, which are done already.
  After ruff's pass the findings are re-read from its re-scan (`DeterministicResult.remaining`),
  since removed imports shift everything up; fingerprints don't change, so pre-class still applies.
- **Triage sees every remaining finding**, including always-escalate ones: clamp overrides it,
  but its reason/risk notes become the report's "proposed approach", and it's what
  `triage_policy_disagreement` and eval C measure. On the fixture it's 9 cheap calls.
- `suggest` findings also get a Fixer diff, shown in the report and restored in the workspace, so
  only `auto_fix` changes are in the patch. `apply_edits` now returns `{file: (before, after)}`
  for exactly one fix; that is what `check_diff` judges and what a rejected fix is restored from.
- Failure handling, all ending in escalated/failed, never in an unchecked change:
  edit doesn't apply or no blocks → one retry with the error fed back, then `failed`;
  `check_diff` violation → restored, `violations` row, escalated; invalid JSON → one repair turn,
  then `invalid_json_from_llm`; `finish_reason=length` with no content → `llm_truncated`;
  429/5xx/connection → 3 attempts with backoff; budget checked before every call (a run can
  overshoot by one call), then all remaining findings escalate and the run outcome is
  `budget_exceeded`.
- `llm.py`: a `Transport` protocol (OpenRouter via the `openai` client with `max_retries=0`, so
  every retry is ours and traced) under `LLMClient`. Tests replace only the transport
  (`tests/fake_llm.py`), so retry/repair/budget code is what's tested. The OpenRouter transport is
  tested against `httpx.MockTransport` (request body: `reasoning.enabled`, `usage.include`,
  `response_format`). New config key `reasoning` per role, default off.
- Prompts: `agents/prompts/{triage,fixer}.md`; `prompt_version` = `PROMPT_VERSION` + hash of the
  prompt files. Repo text goes inside `<untrusted_repo_content>`; a closing tag inside the text is
  escaped so a file can't end the block early. The Fixer gets the whole file without line numbers
  (so SEARCH copies cleanly; a ±80-line window above 400 lines); Triage gets ±20 numbered lines.
  Rule docs come from `ruff rule CODE`.
- Trace store: tables `runs, steps, findings, violations, flags` (flags filled in M4). Steps nest
  `finding → triage → llm:triage` etc. via `parent_step_id`. Every text column goes through
  `redact` (the key's value, `sk-or-…`, `Bearer …`).
- **Pitfall found while testing:** the first CLI test run went through the real `make_client`,
  which loaded the project's `.env` and tried OpenRouter (it failed at the sandbox's TLS proxy, so
  nothing was sent or spent). Now `tests/conftest.py` removes the key, disables `load_dotenv`
  and runs every test in a temp cwd, so a test can't reach the network or write into the project.
- 184 tests, 97% coverage (llm 98%, graph 99%, store 98%, pipeline 100%).
