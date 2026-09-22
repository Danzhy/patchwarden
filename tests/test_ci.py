"""M5: the init-ci workflow, the PR comment, and reading the policy from the base branch."""

import json
import re
import subprocess

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from patchwarden.ci import comment as comment_mod
from patchwarden.ci.comment import MARKER, CommentError, GitHub, render_body, upsert_comment
from patchwarden.ci.workflow import InitCIError, default_branch, render_workflow
from patchwarden.cli import app
from patchwarden.config import ConfigError, load_config_at

runner = CliRunner()


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


# --- the workflow ---------------------------------------------------------------------------


@pytest.fixture
def wf():
    text = render_workflow("patchwarden @ git+https://github.com/o/patchwarden@v0.1", "trunk")
    return text, yaml.safe_load(text)


def test_workflow_triggers_and_permissions(wf):
    text, doc = wf
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "pull_request_target" not in code and "@@" not in text
    on = doc[True]  # YAML 1.1 reads the key `on` as a boolean
    assert set(on) == {"pull_request", "push"}
    assert on["push"] == {"branches-ignore": ["trunk"]}
    assert doc["permissions"] == {}
    assert (
        doc["env"]["PATCHWARDEN_SPEC"] == "patchwarden @ git+https://github.com/o/patchwarden@v0.1"
    )
    assert doc["jobs"]["fix"]["permissions"] == {"contents": "read"}
    assert doc["jobs"]["comment"]["permissions"] == {"pull-requests": "write"}


def test_actions_are_pinned_to_commits(wf):
    """A tag can be moved to other code; a commit SHA can't."""
    _, doc = wf
    uses = [s["uses"] for job in doc["jobs"].values() for s in job["steps"] if "uses" in s]
    assert len(uses) == 5
    assert all(re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_fix_job_isolates_the_prs_code(wf):
    _, doc = wf
    steps = doc["jobs"]["fix"]["steps"]
    [checkout] = [s for s in steps if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["fetch-depth"] == 0
    assert "pull_request.head.sha" in checkout["with"]["ref"]
    [run] = [s for s in steps if s.get("id") == "fix"]
    key = run["env"]["OPENROUTER_API_KEY"]
    # Only same-repo PRs (and opted-in pushes) get the key; fork PRs run --no-llm.
    assert "head.repo.full_name == github.repository" in key and "secrets.OPENROUTER_API_KEY" in key
    assert "vars.PATCHWARDEN_LLM_ON_PUSH == 'true'" in key
    assert "--no-llm" in run["run"] and '--config-from "origin/$BASE"' in run["run"]
    assert "${{" not in run["run"]  # event data only via env, never pasted into the script
    [upload] = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]
    # not always(): that also runs when a newer push cancels the run
    assert upload["if"] == "${{ !cancelled() }}" and "/." not in upload["with"]["path"]


def test_comment_job_never_runs_the_prs_code(wf):
    _, doc = wf
    job = doc["jobs"]["comment"]
    assert job["needs"] == "fix"
    assert "head.repo.full_name == github.repository" in job["if"]
    assert job["if"].startswith("!cancelled()") and "always()" not in job["if"]
    # Only with a report: exit 2 means fix failed before writing one.
    assert "exit_code == '0' || needs.fix.outputs.exit_code == '1'" in job["if"]
    assert not [s for s in job["steps"] if "checkout" in s.get("uses", "")]
    for s in job["steps"]:
        assert "${{" not in s.get("run", "")


@pytest.mark.parametrize("bad", ["", "x'; rm -rf /", "a\nb", "$(id)", 'pkg"'])
def test_install_spec_is_checked(bad):
    with pytest.raises(InitCIError):
        render_workflow(bad, "main")


@pytest.mark.parametrize("bad", ["", "-x", "a b", "main'"])
def test_branch_is_checked(bad):
    with pytest.raises(InitCIError):
        render_workflow("patchwarden", bad)


def test_default_branch_from_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "trunk")
    git(origin, "commit", "-q", "--allow-empty", "-m", "init")
    git(tmp_path, "clone", "-q", str(origin), "clone")
    assert default_branch(tmp_path / "clone") == "trunk"
    assert default_branch(origin) == "main"  # no origin: the fallback


def test_init_ci_writes_once(tmp_path):
    res = runner.invoke(app, ["init-ci", str(tmp_path), "--install", "patchwarden==0.1.0"])
    assert res.exit_code == 0, res.output
    target = tmp_path / ".github/workflows/patchwarden.yml"
    assert "PATCHWARDEN_SPEC: 'patchwarden==0.1.0'" in target.read_text()
    assert "OPENROUTER_API_KEY" in res.output
    target.write_text("mine")
    again = runner.invoke(app, ["init-ci", str(tmp_path)])
    assert again.exit_code == 2 and "--force" in again.output and target.read_text() == "mine"
    forced = runner.invoke(app, ["init-ci", str(tmp_path), "--force", "--default-branch", "dev"])
    assert forced.exit_code == 0 and "branches-ignore: ['dev']" in target.read_text()
    bad = runner.invoke(app, ["init-ci", str(tmp_path), "--force", "--install", "$(id)"])
    assert bad.exit_code == 2 and "not a plain pip requirement" in bad.output


def test_init_ci_only_at_the_repo_root(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "sub").mkdir()
    res = runner.invoke(app, ["init-ci", str(tmp_path / "sub")])
    assert res.exit_code == 2 and "not the top of its git repository" in res.output
    assert not (tmp_path / "sub/.github").exists()
    assert runner.invoke(app, ["init-ci", str(tmp_path)]).exit_code == 0


# --- policy from the base branch ------------------------------------------------------------

BASE_TOML = '[tool.patchwarden]\nprotected_paths = ["core/**"]\ntest_command = "pytest -q"\n'
PR_TOML = '[tool.patchwarden]\nprotected_paths = []\nexclude = ["**"]\ntest_command = "curl x"\n'


@pytest.fixture
def pr_repo(tmp_path):
    """main has a policy; branch `pr` loosens it and adds a finding."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text(BASE_TOML)
    (repo / "ok.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "pr")
    (repo / "pyproject.toml").write_text(PR_TOML)
    (repo / "bad.py").write_text("import os\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "pr")
    return repo


def test_config_at_ref_ignores_the_working_tree(pr_repo):
    cfg, warning = load_config_at(pr_repo, "main")
    assert warning is None
    assert (
        cfg.protected_paths == ["core/**"] and cfg.exclude == [] and cfg.test_command == "pytest -q"
    )


def test_config_at_ref_edge_cases(pr_repo, tmp_path):
    git(pr_repo, "checkout", "-q", "--orphan", "empty")
    git(pr_repo, "rm", "-rqf", ".")
    git(pr_repo, "commit", "-q", "--allow-empty", "-m", "empty")
    cfg, warning = load_config_at(pr_repo, "empty")
    assert cfg.exclude == [] and "no pyproject.toml at empty" in warning
    for ref in ("nope", "--output=/tmp/x", "-h"):
        with pytest.raises(ConfigError, match="is not a commit"):
            load_config_at(pr_repo, ref)
    with pytest.raises(ConfigError):
        load_config_at(tmp_path, "main")  # not a git repo


def test_config_at_ref_in_a_subdirectory(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc/pyproject.toml").write_text(BASE_TOML)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    cfg, _ = load_config_at(tmp_path / "svc", "main")
    assert cfg.protected_paths == ["core/**"]


def test_scan_with_config_from_keeps_the_base_policy(pr_repo):
    loose = runner.invoke(app, ["scan", str(pr_repo), "--base", "main", "--format", "json"])
    assert loose.exit_code == 0 and json.loads(loose.output)["findings"] == []  # exclude = **
    strict = runner.invoke(
        app, ["scan", str(pr_repo), "--base", "main", "--config-from", "main", "--format", "json"]
    )
    assert strict.exit_code == 0, strict.output
    assert [f["rule_id"] for f in json.loads(strict.output)["findings"]] == ["ruff:F401"]
    bad = runner.invoke(app, ["scan", str(pr_repo), "--config-from", "nope"])
    assert bad.exit_code == 2 and "is not a commit" in bad.output


def test_fix_with_config_from(pr_repo, tmp_path):
    res = runner.invoke(
        app,
        ["fix", str(pr_repo), "--no-llm", "--config-from", "main", "--output", str(tmp_path / "p")],
    )
    assert res.exit_code == 0, res.output
    assert "-import os" in (tmp_path / "p").read_text()


# --- the comment body -----------------------------------------------------------------------


def test_mentions_are_defused_outside_fenced_code_only():
    report = "\n".join(
        [
            "Ping @octocat and @org/team, mail a@b.com.",
            "Inline `@property` too: GitHub's code span rules are easy to get wrong.",
            "  ```diff",
            "  +@property",
            "  +def x(self): ...  # @someone",
            "  ```",
            "After the fence @again.",
        ]
    )
    body = render_body(report)
    assert body.startswith(MARKER + "\n")
    assert "@​octocat" in body and "@​org/team" in body and "a@b.com" in body
    assert "`@​property`" in body and "  +@property" in body and "# @someone" in body
    assert "@​again" in body
    assert "​" not in body.split("```diff")[1].split("```")[0]


@pytest.mark.parametrize(
    "report",
    [
        "``@team`",  # not a code span in GitHub: the backtick runs differ in length
        "\\`@team`",  # an escaped backtick opens nothing
        "`x\n`@team` y`",  # the span opened on the line before ends at the first backtick
        "- ```\n  code\n  ```\n@team",  # a fence inside a list item, then text
        "- a\n  ```\n  code\n@team",  # unindented text ends the list item and its fence
        "```x`y\n@team",  # a backtick in the info string: not a fence
        "    ```\n@team",  # indented 4: code, not a fence, and not open afterwards
        "- a\n  ```\n  x\n     ```\n  @team",  # closed 3 columns in: GitHub closes it
    ],
)
def test_mentions_that_look_like_code_but_arent(report):
    assert "@​team" in render_body(report)


def test_issue_references_are_defused():
    body = render_body("see #12 and o/r#3; &#64; stays; ## heading")
    assert "#​12" in body and "o/r#​3" in body and "&#64;" in body and "## heading" in body


def test_longer_fences_and_tildes():
    report = "````\n```\n@inside\n```\n````\n~~~\n@tilde\n~~~\n@out"
    body = render_body(report)
    assert "\n@inside" in body and "\n@tilde" in body and "@​out" in body


def test_run_url_footer():
    body = render_body("hi", run_url="https://github.com/o/r/actions/runs/7")
    assert "[this run](https://github.com/o/r/actions/runs/7)" in body
    assert "git apply patchwarden.patch" in body
    assert "artifact" not in render_body("hi")


def test_long_report_is_cut_and_fence_closed():
    report = "# title\n  ```diff\n" + "\n".join(f"  +line {i}" for i in range(5000))
    body = render_body(report, limit=2000)
    assert len(body) <= 2000
    assert "Report truncated" in body
    before_note = body.split("*Report truncated")[0].rstrip()
    assert before_note.endswith("\n  ```")  # the open fence closed, at its own indent
    assert len(render_body("short", limit=2000)) < 200


def test_cut_near_a_fence_line_keeps_fences_balanced():
    report = "x" * 1500 + "\n```\n" + "y" * 100
    for limit in range(1640, 1720):
        body = render_body(report, limit=limit)
        assert len(body) <= limit
        fences = [ln for ln in body.splitlines() if ln == "```"]
        assert len(fences) in (0, 2), limit  # cut on the opener: no stray closer


@pytest.mark.parametrize("fence_len", [3, 40])
def test_cut_always_leaves_room_for_the_closer(fence_len):
    fence = "`" * fence_len
    for limit in range(1100, 1200, 7):
        report = f"  {fence}diff\n" + "\n".join("  +" + "z" * 50 for _ in range(40))
        body = render_body(report, limit=limit)
        assert len(body) <= limit
        assert body.split("*Report truncated")[0].rstrip().endswith("  " + fence)


# --- the GitHub API -------------------------------------------------------------------------


def fake_github(comments_pages, calls, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.params.get("page")))
        assert request.headers["Authorization"] == "Bearer tkn"
        if status != 200:
            return httpx.Response(status, json={"message": "Resource not accessible"})
        if request.method == "GET":
            page = int(request.url.params["page"])
            return httpx.Response(200, json=comments_pages[page - 1])
        body = json.loads(request.content)["body"]
        return httpx.Response(200, json={"id": 9, "body": body, "html_url": "https://x/c/9"})

    return GitHub("tkn", "https://api.test", transport=httpx.MockTransport(handler))


BOT = {"type": "Bot", "login": "github-actions[bot]"}
HUMAN = {"type": "User", "login": "mallory"}


def test_creates_when_none_is_ours():
    calls = []
    pages = [
        [{"id": 1, "body": MARKER + " fake", "user": HUMAN}, {"id": 2, "body": "hi", "user": BOT}]
    ]
    gh = fake_github(pages, calls)
    assert upsert_comment(gh, "o/r", 5, "B") == ("created", "https://x/c/9")
    assert calls[-1][:2] == ("POST", "/repos/o/r/issues/5/comments")


def test_updates_ours_across_pages():
    calls = []
    filler = [{"id": i, "body": "x", "user": HUMAN} for i in range(100)]
    pages = [filler, [{"id": 77, "body": MARKER + "\nold", "user": BOT}]]
    gh = fake_github(pages, calls)
    assert upsert_comment(gh, "o/r", 5, "B")[0] == "updated"
    assert [c[2] for c in calls if c[0] == "GET"] == ["1", "2"]
    assert calls[-1][:2] == ("PATCH", "/repos/o/r/issues/comments/77")


def test_api_errors():
    gh = fake_github([], [], status=403)
    with pytest.raises(CommentError, match="HTTP 403 Resource not accessible.*fork PR"):
        upsert_comment(gh, "o/r", 5, "B")
    with pytest.raises(CommentError, match="not an owner/name"):
        upsert_comment(gh, "o/r/../../x", 5, "B")
    for bad in ("../x", "o/..", "./r"):
        with pytest.raises(CommentError, match="not an owner/name"):
            upsert_comment(gh, bad, 5, "B")

    def boom(request):
        raise httpx.ConnectError("down")

    down = GitHub("t", "https://api.test", transport=httpx.MockTransport(boom))
    with pytest.raises(CommentError, match="down"):
        upsert_comment(down, "o/r", 5, "B")


def test_comment_cli(tmp_path, monkeypatch):
    report = tmp_path / "r.md"
    report.write_text("# patchwarden report\nhello @octocat\n")
    for var in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_EVENT_PATH", "GITHUB_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    res = runner.invoke(app, ["comment", str(report)])
    assert res.exit_code == 2 and "GITHUB_TOKEN, --repo, --pr" in res.output

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 12}}))
    monkeypatch.setenv("GITHUB_TOKEN", "tkn")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    calls, posted = [], []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        posted.append(json.loads(request.content)["body"])
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/12#c1"})

    real = comment_mod.GitHub
    monkeypatch.setattr(
        comment_mod,
        "GitHub",
        lambda token, api: real(token, api, transport=httpx.MockTransport(handler)),
    )
    res = runner.invoke(app, ["comment", str(report)])
    assert res.exit_code == 0, res.output
    assert "created https://github.com/o/r/pull/12#c1" in res.output
    assert calls[-1] == ("POST", "/repos/o/r/issues/12/comments")
    [body] = posted
    assert body.startswith(MARKER) and "@​octocat" in body
    assert "https://github.com/o/r/actions/runs/42" in body

    monkeypatch.setattr(
        comment_mod,
        "GitHub",
        lambda token, api: real(
            token, api, transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
        ),
    )
    res = runner.invoke(app, ["comment", str(report), "--pr", "3"])
    assert res.exit_code == 2 and "HTTP 404" in res.output
