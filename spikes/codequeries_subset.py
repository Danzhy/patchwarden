"""M0 spike: inspect CodeQueries (file_ideal split) and choose the eval subset for benchmark A.

  uv run python spikes/codequeries_subset.py stats        # counts per query + CodeQL id mapping
  uv run python spikes/codequeries_subset.py select       # write eval/subsets/codequeries.json

Needs eval/data/file_ideal_test.json (HF thepurpleowl/codequeries, Apache-2.0), the ETH Py150
source files under eval/data/py150/data (see NOTES.md), and a CodeQL checkout at $CODEQL_REPO
(default ~/codeql-home/codeql-repo) to map query names to ids.
"""

import ast
import hashlib
import json
import os
import random
import re
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "eval/data/file_ideal_test.json"
OUT = ROOT / "eval/subsets/codequeries.json"
PY150 = ROOT / "eval/data/py150/data"  # files.sri.inf.ethz.ch/data/py150_files.tar.gz
CODEQL_REPO = Path(os.environ.get("CODEQL_REPO", Path.home() / "codeql-home/codeql-repo"))

# Rules chosen by hand from `stats`: common, fixable inside one file, and still present in the
# current CodeQL Python pack. Flask debug mode is a security rule policy must escalate.
# Dropped after the reproduction check: "Module is imported with 'import' and 'import from'"
# (6/20) and "Module is imported more than once" (2/20); both need cross-module resolution.
RULES: list[str] = [
    "Unused import",
    "Unused local variable",
    "Except block handles 'BaseException'",
    "Variable defined multiple times",
    "Unreachable code",
    "Testing equality to None",
    "Unnecessary pass",
    "Import of deprecated module",
    "'import *' may pollute namespace",
    "Redundant assignment",
    "Unnecessary 'else' clause in loop",
    "Flask app is run in debug mode",
]

SEED = 20260921
MAX_FILES_PER_RULE = 20
MAX_FILE_LINES = 400  # keep the Fixer's input to a whole file for most cases
DEV_FRACTION = 0.3


def codeql_ids() -> dict[str, tuple[str, str]]:
    """Map query @name -> (@id, path relative to the python pack)."""
    src = CODEQL_REPO / "python/ql/src"
    out = {}
    for ql in src.rglob("*.ql"):
        text = ql.read_text(errors="replace")
        name = re.search(r"@name\s+(.+)", text)
        qid = re.search(r"@id\s+(\S+)", text)
        if name and qid:
            out[name.group(1).strip()] = (qid.group(1), str(ql.relative_to(src)))
    return out


def rows():
    with DATA.open() as f:
        for line in f:
            yield json.loads(line)


def positive(row) -> bool:
    return bool(row["answer_spans"])


def whole_file(row) -> str | None:
    """The original file from ETH Py150 if it parses as Python 3 and matches the labels, else None.

    CodeQueries rows hold only the blocks relevant to the query, with blank placeholders where
    other functions and classes go, so they can't be re-scanned. Py150 has the whole files.
    Much of the corpus is Python 2, which we skip.
    """
    path = PY150 / row["code_file_path"]
    if not path.is_file():
        return None
    src = path.read_text(errors="replace")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            ast.parse(src)
        except SyntaxError:
            return None
    lines = src.split("\n")
    for span in row["answer_spans"]:
        first = span["span"].split("\n")[0].strip()
        if span["start_line"] >= len(lines) or first not in lines[span["start_line"]]:
            return None
    return src


def stats() -> None:
    ids = codeql_ids()
    pos, neg, lines = Counter(), Counter(), defaultdict(list)
    for row in rows():
        q = row["query_name"]
        if positive(row):
            pos[q] += 1
            content = "".join(b["content"] for b in row["context_blocks"])
            lines[q].append(content.count("\n") + 1)
        else:
            neg[q] += 1
    print(f"{'query':55} {'pos':>5} {'neg':>5} {'med_lines':>9}  codeql id")
    for q, n in pos.most_common():
        ls = sorted(lines[q])
        qid = ids.get(q, ("MISSING", ""))[0]
        print(f"{q[:55]:55} {n:5} {neg[q]:5} {ls[len(ls) // 2]:9}  {qid}")
    print("total queries:", len(pos | neg), "| positive rows:", sum(pos.values()))


def select() -> None:
    if not RULES:
        sys.exit("Fill RULES first (run `stats`).")
    ids = codeql_ids()
    missing = [r for r in RULES if r not in ids]
    if missing:
        sys.exit(f"Not in the CodeQL pack: {missing}")
    by_rule = defaultdict(list)
    for row in rows():
        q = row["query_name"]
        if q not in RULES or not positive(row):
            continue
        content = whole_file(row)
        if content is None or content.count("\n") + 1 > MAX_FILE_LINES:
            continue
        by_rule[q].append(
            {
                "code_file_path": row["code_file_path"],
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                # CodeQueries lines are 0-based; stored 1-based to match SARIF.
                "answer_lines": sorted({s["start_line"] + 1 for s in row["answer_spans"]}),
            }
        )
    rng = random.Random(SEED)
    items = []
    for q in RULES:
        cands = sorted(by_rule[q], key=lambda x: x["code_file_path"])
        rng.shuffle(cands)
        for c in cands[:MAX_FILES_PER_RULE]:
            items.append({"query_name": q, "codeql_id": ids[q][0], "query_path": ids[q][1], **c})
    # Split by source repo so near-duplicate files from one project never straddle dev and test.
    repos = sorted({"/".join(i["code_file_path"].split("/")[:2]) for i in items})
    rng.shuffle(repos)
    dev_repos = set(repos[: int(len(repos) * DEV_FRACTION)])
    for i in items:
        repo = "/".join(i["code_file_path"].split("/")[:2])
        i["split"] = "dev" if repo in dev_repos else "test"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "source": "huggingface:thepurpleowl/codequeries file_ideal test (Apache-2.0)",
                "seed": SEED,
                "max_files_per_rule": MAX_FILES_PER_RULE,
                "max_file_lines": MAX_FILE_LINES,
                "items": items,
            },
            indent=1,
        )
    )
    split = Counter((i["split"]) for i in items)
    print(f"wrote {len(items)} items to {OUT.relative_to(ROOT)}: {dict(split)}")
    for q in RULES:
        print(f"  {q}: {sum(1 for i in items if i['query_name'] == q)}")


if __name__ == "__main__":
    {"stats": stats, "select": select}[sys.argv[1] if len(sys.argv) > 1 else "stats"]()
