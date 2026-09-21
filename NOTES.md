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
- **Not run yet:** there's no `OPENROUTER_API_KEY` in the environment or `.env`.
- Default model candidates from the public model list (price per M tokens, input/output), used
  only as config defaults and always overridable:
  - triage (cheap): `deepseek/deepseek-v4.1-flash` ($0.15 / $0.60)
  - fixer and verifier (strong): `anthropic/claude-sonnet-5` ($2 / $10)
- Still to confirm: `response_format=json_object` is honoured, and `usage` includes `cost` when
  `usage: {include: true}` is sent.
