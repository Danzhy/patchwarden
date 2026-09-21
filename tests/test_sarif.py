import json
from pathlib import Path

from patchwarden.analyzers.sarif import normalize_ws, parse_sarif, relative_path

FIXTURES = Path(__file__).parent / "fixtures"
REPO = FIXTURES / "repo_small"


def load(name: str) -> dict:
    return json.loads((FIXTURES / "sarif" / f"{name}.sarif").read_text())


def test_ruff_sarif():
    findings = parse_sarif(load("ruff"), "ruff", REPO)
    assert len(findings) == 12
    assert all(f.rule_id.startswith("ruff:") for f in findings)
    f401 = [f for f in findings if f.rule_id == "ruff:F401"]
    assert {f.file for f in f401} == {"app/utils.py", "app/auth/tokens.py"}
    # Ruff has no snippet in SARIF: it's read from the file.
    os_import = next(f for f in f401 if f.file == "app/utils.py" and f.region.start_line == 1)
    assert os_import.snippet == "import os\n"
    assert os_import.tool_fixable


def test_bandit_sarif():
    findings = parse_sarif(load("bandit"), "bandit", REPO)
    assert sorted(f.rule_id for f in findings) == ["bandit:B324", "bandit:B404", "bandit:B602"]
    b602 = next(f for f in findings if f.rule_id == "bandit:B602")
    assert b602.file == "app/runner.py"
    assert "shell=True" in b602.snippet
    assert (b602.severity, b602.confidence) == ("high", "high")
    assert not b602.tool_fixable


def test_codeql_sarif():
    findings = parse_sarif(load("codeql"), "codeql", REPO)
    # The second result points outside the repo and is dropped.
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "codeql:py/unused-import"
    assert f.region.end_line == f.region.start_line == 1  # endLine omitted in SARIF
    assert f.snippet == "import os\n"


def test_absolute_file_uri(tmp_path):
    (tmp_path / "a b.py").write_text("x = 1\n")
    assert relative_path((tmp_path / "a b.py").as_uri(), tmp_path) == "a b.py"
    assert relative_path("a%20b.py", tmp_path) == "a b.py"
    assert relative_path("file:///etc/passwd", tmp_path) is None


def _doc(uri: str, line: int, rule: str = "F401") -> dict:
    return {
        "runs": [
            {
                "results": [
                    {
                        "ruleId": rule,
                        "message": {"text": "m"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": uri},
                                    "region": {"startLine": line},
                                }
                            }
                        ],
                    }
                ]
            }
        ]
    }


def test_fingerprint_stable_across_line_shift(tmp_path):
    (tmp_path / "m.py").write_text("import os\n")
    before = parse_sarif(_doc("m.py", 1), "ruff", tmp_path)[0]
    (tmp_path / "m.py").write_text("# header\n\n\nimport   os\n")
    after = parse_sarif(_doc("m.py", 4), "ruff", tmp_path)[0]
    assert before.fingerprint == after.fingerprint
    assert before.region.start_line != after.region.start_line


def test_fingerprint_changes_with_snippet_rule_or_file(tmp_path):
    (tmp_path / "m.py").write_text("import os\nimport sys\n")
    (tmp_path / "n.py").write_text("import os\n")
    base = parse_sarif(_doc("m.py", 1), "ruff", tmp_path)[0].fingerprint
    assert parse_sarif(_doc("m.py", 2), "ruff", tmp_path)[0].fingerprint != base
    assert parse_sarif(_doc("m.py", 1, "E401"), "ruff", tmp_path)[0].fingerprint != base
    assert parse_sarif(_doc("n.py", 1), "ruff", tmp_path)[0].fingerprint != base


def test_duplicate_snippets_get_unique_fingerprints(tmp_path):
    (tmp_path / "m.py").write_text("x == None\nx == None\nx == None\n")
    doc = _doc("m.py", 1, "E711")
    res = doc["runs"][0]["results"][0]
    doc["runs"][0]["results"] = [
        json.loads(json.dumps(res).replace('"startLine": 1', f'"startLine": {n}'))
        for n in (3, 1, 2)
    ]
    fps = {f.region.start_line: f.fingerprint for f in parse_sarif(doc, "ruff", tmp_path)}
    assert len(set(fps.values())) == 3
    assert fps[2] == fps[1] + ":2" and fps[3] == fps[1] + ":3"


def test_normalize_ws():
    assert normalize_ws("  a\t b\n\n c ") == "a b c"
