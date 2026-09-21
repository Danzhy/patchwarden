"""M0 spike: run ruff and bandit with SARIF output on the fixture repo and summarise the shape."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "tests/fixtures/repo_small"
OUT = ROOT / "tests/fixtures/sarif"
BIN = Path(sys.executable).parent

CMDS = {
    "ruff": [
        str(BIN / "ruff"),
        "check",
        "--isolated",
        "--no-cache",
        "--select",
        "E,F,B,UP,SIM",
        "--output-format",
        "sarif",
        ".",
    ],
    "bandit": [str(BIN / "bandit"), "-q", "-r", "app", "-f", "sarif"],
}

for name, cmd in CMDS.items():
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    sarif = json.loads(proc.stdout)
    # Store fixtures with repo-relative URIs so they don't embed this machine's paths.
    text = json.dumps(sarif, indent=2).replace(REPO.as_uri() + "/", "")
    (OUT / f"{name}.sarif").write_text(text)
    run = sarif["runs"][0]
    results = run["results"]
    print(
        f"== {name}: exit={proc.returncode} version={sarif.get('version')}",
        f"results={len(results)}",
    )
    print(
        "  tool:",
        run["tool"]["driver"]["name"],
        "| rules in driver:",
        len(run["tool"]["driver"].get("rules", [])),
    )
    r = results[0]
    print("  result keys:", sorted(r))
    loc = r["locations"][0]["physicalLocation"]
    print("  physicalLocation keys:", sorted(loc), "| region keys:", sorted(loc["region"]))
    print("  uri example:", loc["artifactLocation"]["uri"])
    print("  snippet present:", "snippet" in loc["region"])
    print(
        "  partialFingerprints:",
        r.get("partialFingerprints"),
        "| fingerprints:",
        r.get("fingerprints"),
    )
    print("  level/properties:", r.get("level"), r.get("properties"))
    print("  ruleIds:", sorted({x["ruleId"] for x in results}))
