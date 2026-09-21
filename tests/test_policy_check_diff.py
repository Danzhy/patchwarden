import pytest

from patchwarden.config import Config
from patchwarden.models import ViolationKind
from patchwarden.policy import changed_line_count, check_diff

K = ViolationKind

BEFORE = """import os
import sys


class Square:
    def area(self):
        return 1


def append_item(item, bucket=[]):
    bucket.append(item)
    return bucket


def total(items: "List[int]") -> int:
    return sum(items)
"""


def kinds(after, *, file="app/utils.py", before=BEFORE, rule="ruff:F401", max_lines=30):
    changes = {file: (before, after)}
    found = check_diff(
        changes, Config(), target_file="app/utils.py", rule_ids={rule}, max_lines=max_lines
    )
    return sorted(v.kind for v in found)


def test_clean_fix_has_no_violations():
    assert kinds(BEFORE.replace("import os\n", "")) == []


def test_annotation_change_allowed():
    assert kinds(BEFORE.replace('"List[int]"', "list[int]"), rule="ruff:UP006") == []


@pytest.mark.parametrize(
    "suppressed",
    [
        "import os  # noqa: F401\n",
        "import os  # NOQA\n",
        "import os  # type: ignore\n",
        "import os  # nosec\n",
        "import os  # pragma: no cover\n",
        "import os  # pylint: disable=unused-import\n",
        "import os  # lgtm[py/unused-import]\n",
        "import os  # codeql[py/unused-import]\n",
    ],
)
def test_suppression_added(suppressed):
    assert kinds(BEFORE.replace("import os\n", suppressed)) == [K.suppression_added]


def test_existing_suppression_is_not_new():
    before = BEFORE.replace("import sys\n", "import sys  # noqa\n")
    after = before.replace("import os\n", "")
    assert kinds(after, before=before) == []


def test_noqa_inside_a_string_is_not_a_comment():
    after = BEFORE.replace("import os\n", 'MSG = "# noqa"\n')
    assert kinds(after) == []


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        ("tests/test_utils.py", [K.other_file_touched, K.test_file_touched]),
        ("app/auth/tokens.py", [K.other_file_touched, K.protected_file_touched]),
        ("app/other.py", [K.other_file_touched]),
    ],
)
def test_scope_violations(file, expected):
    assert kinds(BEFORE.replace("import os\n", ""), file=file) == sorted(expected)


def test_too_many_lines():
    after = BEFORE + "".join(f"x{i} = {i}\n" for i in range(31))
    assert kinds(after) == [K.too_many_lines]
    assert kinds(after, max_lines=None) == []


def test_syntax_error():
    assert kinds(BEFORE.replace("return 1", "return (")) == [K.syntax_error]


def test_definition_removed():
    after = BEFORE.replace("    def area(self):\n        return 1\n", "    pass\n")
    assert kinds(after) == [K.definition_removed]
    after = BEFORE.replace("class Square:", "class Rect:")
    assert kinds(after) == [K.definition_removed, K.definition_removed]


@pytest.mark.parametrize(
    "new_sig",
    [
        "def append_item(item, bin=[]):",  # renamed
        "def append_item(bucket, item=[]):",  # reordered
        "def append_item(item, *, bucket=[]):",  # kind changed
        "def append_item(item, bucket=[], extra=0):",  # added
    ],
)
def test_parameter_changes_are_violations(new_sig):
    after = BEFORE.replace("def append_item(item, bucket=[]):", new_sig)
    assert kinds(after, rule="ruff:B006") == [K.signature_changed]


def test_default_change_allowed_only_for_signature_rules():
    after = BEFORE.replace("bucket=[]):", "bucket=None):")
    assert kinds(after, rule="ruff:B006") == []
    assert kinds(after, rule="ruff:F841") == [K.signature_changed]


def test_unparseable_before_skips_ast_checks():
    assert kinds("x = 1\n", before="def (:\n") == []


def test_changed_line_count():
    assert changed_line_count("a\nb\nc\n", "a\nB\nc\nd\n") == 3
