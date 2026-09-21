import pytest

from patchwarden.edits import (
    EditApplyError,
    EditParseError,
    apply_block,
    apply_edits,
    parse_edit_blocks,
)
from patchwarden.models import EditBlock
from patchwarden.workspace import open_workspace

TWO_BLOCKS = """Here is the fix.

app/utils.py
```python
<<<<<<< SEARCH
import os
=======
>>>>>>> REPLACE
```

app/utils.py
<<<<<<< SEARCH
    if value == None:
=======
    if value is None:
>>>>>>> REPLACE
"""


def test_parse_two_blocks_with_fence_and_prose():
    blocks = parse_edit_blocks(TWO_BLOCKS)
    assert blocks == [
        EditBlock(file="app/utils.py", search="import os\n", replace=""),
        EditBlock(
            file="app/utils.py",
            search="    if value == None:\n",
            replace="    if value is None:\n",
        ),
    ]


def test_parse_no_blocks():
    assert parse_edit_blocks("no edits needed") == []


@pytest.mark.parametrize(
    "text",
    [
        "<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n",  # no path
        "a.py\n<<<<<<< SEARCH\nx\n=======\ny\n",  # unterminated
        "a.py\n<<<<<<< SEARCH\nx\n>>>>>>> REPLACE\n",  # no divider
    ],
)
def test_parse_malformed(text):
    with pytest.raises(EditParseError):
        parse_edit_blocks(text)


def block(search, replace="y\n"):
    return EditBlock(file="a.py", search=search, replace=replace)


def test_apply_exact():
    assert apply_block("a\nx\nb\n", block("x\n")) == "a\ny\nb\n"


@pytest.mark.parametrize(
    ("text", "search", "kind"),
    [
        ("a\nb\n", "x\n", "no_match"),
        ("x\nx\n", "x\n", "ambiguous"),
        ("x \nx\t\n", "x\n", "ambiguous"),  # loose match, twice
        ("a\n", "  \n", "empty_search"),
    ],
)
def test_apply_errors(text, search, kind):
    with pytest.raises(EditApplyError) as e:
        apply_block(text, block(search))
    assert e.value.kind == kind


def test_trailing_whitespace_fallback():
    assert apply_block("a\nx   \nb\n", block("x\n")) == "a\ny\nb\n"
    # Last line with no newline still matches.
    assert apply_block("a\nx", block("x\n")) == "a\ny\n"


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("one\ntwo\n")
    (repo / "b.py").write_text("three\n")
    return repo


def test_apply_edits_sequential(tmp_path):
    with open_workspace(make_repo(tmp_path)) as ws:
        changed = apply_edits(
            ws,
            [
                EditBlock(file="a.py", search="one\n", replace="ONE\n"),
                EditBlock(file="a.py", search="ONE\ntwo\n", replace="done\n"),
            ],
        )
        assert changed == {"a.py": ("one\ntwo\n", "done\n")}
        assert ws.read("a.py") == "done\n"


@pytest.mark.parametrize("path", ["/etc/passwd", "../x.py", "missing.py", "notes.txt"])
def test_bad_path(tmp_path, path):
    with open_workspace(make_repo(tmp_path)) as ws:
        with pytest.raises(EditApplyError) as e:
            apply_edits(ws, [EditBlock(file=path, search="x\n", replace="")])
        assert e.value.kind == "bad_path"


def test_all_or_nothing(tmp_path):
    with open_workspace(make_repo(tmp_path)) as ws:
        with pytest.raises(EditApplyError):
            apply_edits(
                ws,
                [
                    EditBlock(file="a.py", search="one\n", replace="ONE\n"),
                    EditBlock(file="b.py", search="nope\n", replace=""),
                ],
            )
        assert ws.changes() == {}
