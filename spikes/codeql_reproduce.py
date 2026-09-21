"""M0 spike: check current CodeQL reproduces the CodeQueries labels on the chosen subset.

Writes each subset file into one source tree, builds a single CodeQL DB, runs only the chosen
queries, and reports per rule how many items have a result at a labelled line. Items that don't
reproduce can't be scored with "target gone", so the eval drops them.

  uv run python spikes/codequeries_subset.py select
  uv run python spikes/codeql_reproduce.py
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from codequeries_subset import CODEQL_REPO, OUT, rows, whole_file

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "spikes/out/cq_repro"
CODEQL = os.environ.get("CODEQL", str(Path.home() / "codeql-home/codeql/codeql"))


def main() -> None:
    manifest = json.loads(OUT.read_text())
    items = manifest["items"]
    wanted = {(i["query_name"], i["code_file_path"]): n for n, i in enumerate(items)}

    shutil.rmtree(WORK, ignore_errors=True)
    src_dir = WORK / "src"
    src_dir.mkdir(parents=True)
    for row in rows():
        n = wanted.get((row["query_name"], row["code_file_path"]))
        if n is None or (WORK / "src" / f"item_{n:03}.py").exists():
            continue
        content = whole_file(row)
        if content and hashlib.sha256(content.encode()).hexdigest() == items[n]["sha256"]:
            (src_dir / f"item_{n:03}.py").write_text(content)
    print("materialised", len(list(src_dir.iterdir())), "of", len(items))

    db = WORK / "db"
    t0 = time.monotonic()
    subprocess.run(
        [
            CODEQL,
            "database",
            "create",
            str(db),
            "--language=python",
            f"--source-root={src_dir}",
            "-q",
        ],
        check=True,
    )
    t1 = time.monotonic()
    queries = sorted({str(CODEQL_REPO / "python/ql/src" / i["query_path"]) for i in items})
    sarif = WORK / "results.sarif"
    subprocess.run(
        [
            CODEQL,
            "database",
            "analyze",
            str(db),
            *queries,
            "--format=sarif-latest",
            f"--output={sarif}",
            "--threads=0",
            "-q",
        ],
        check=True,
    )
    t2 = time.monotonic()
    print(f"db create {t1 - t0:.0f}s, analyze {t2 - t1:.0f}s")

    hits = set()
    for r in json.loads(sarif.read_text())["runs"][0]["results"]:
        loc = r["locations"][0]["physicalLocation"]
        hits.add((r["ruleId"], loc["artifactLocation"]["uri"], loc["region"]["startLine"]))

    per_rule, repro = Counter(), Counter()
    for n, i in enumerate(items):
        per_rule[i["query_name"]] += 1
        uri = f"item_{n:03}.py"
        ok = any((i["codeql_id"], uri, line) in hits for line in i["answer_lines"])
        i["reproduces"] = ok
        repro[i["query_name"]] += ok
    for q in per_rule:
        print(f"  {q[:55]:55} {repro[q]:3}/{per_rule[q]}")
    print("total reproduced:", sum(repro.values()), "/", len(items))
    OUT.write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
