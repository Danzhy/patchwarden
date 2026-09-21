"""Configuration: defaults, overridden by `[tool.patchwarden]` in the target repo's pyproject.

Rule patterns are fnmatch globs over namespaced rule ids ("ruff:UP*", "bandit:*"); path
patterns are fnmatch globs over repo-relative POSIX paths (see scope.match_path).
"""

import dataclasses
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    analyzers: list[str] = field(default_factory=lambda: ["ruff", "bandit"])
    ruff_select: list[str] = field(default_factory=lambda: ["E", "F", "B", "UP", "SIM"])
    # Query files/packs/suites for CodeQL; empty means the python-code-scanning suite.
    codeql_queries: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    test_paths: list[str] = field(
        default_factory=lambda: ["tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]
    )
    # Rules whose fixes are mechanical and behaviour-preserving. Kept deliberately short: the
    # allowlist only lowers the floor to auto_fix, verification still has to pass.
    auto_fix: list[str] = field(
        default_factory=lambda: [
            "ruff:F401",  # unused import
            "ruff:F541",  # f-string without placeholders
            "ruff:F841",  # unused local variable
            "ruff:E711",  # comparison to None
            "ruff:E712",  # comparison to True/False
            "ruff:UP*",  # pyupgrade syntax modernisation
            "codeql:py/unused-import",
            "codeql:py/unused-local-variable",
            "codeql:py/test-equals-none",
            "codeql:py/unnecessary-pass",
        ]
    )
    # Security findings always go to a human, whatever the LLM thinks.
    always_escalate: list[str] = field(
        default_factory=lambda: [
            "bandit:*",
            "ruff:S[0-9]*",  # flake8-bandit; not "ruff:S*", which would match SIM
            "codeql:py/*injection*",
            "codeql:py/flask-debug",
            "codeql:py/weak-*",
            "codeql:py/insecure-*",
            "codeql:py/clear-text-*",
            "codeql:py/hardcoded-credentials",
        ]
    )
    protected_paths: list[str] = field(
        default_factory=lambda: [
            "**/auth/**",
            "**/security/**",
            "**/migrations/**",
            ".github/**",
            "**/settings*.py",
        ]
    )
    # Rules whose fix legitimately changes a parameter default (B006: `x=[]` -> `x=None`).
    signature_rules: list[str] = field(
        default_factory=lambda: [
            "ruff:B006",
            "ruff:B008",
            "codeql:py/modification-of-default-value",
        ]
    )
    # Run in the workspace after every fix (no shell, secrets removed from the environment).
    # Without it, Fixer changes are only ever suggested: untested code is never auto-fixed.
    test_command: str | None = None
    test_timeout_s: int = 300
    # Fixer calls per finding, counting the retries after an edit that doesn't apply, a failed
    # check and a Verifier rejection.
    max_fix_rounds: int = 2
    max_lines_changed: int = 30
    budget_usd: float = 0.50
    models: dict[str, str] = field(
        default_factory=lambda: {
            "triage": "deepseek/deepseek-v4.1-flash",
            "fixer": "anthropic/claude-sonnet-5",
            "verifier": "anthropic/claude-sonnet-5",
        }
    )
    # OpenRouter's reasoning switch per role. Off by default: in the M0 spike, reasoning spent
    # every output token and returned no answer.
    reasoning: dict[str, bool] = field(
        default_factory=lambda: {"triage": False, "fixer": False, "verifier": False}
    )

    def config_hash(self) -> str:
        blob = json.dumps(dataclasses.asdict(self), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


_FIELDS = {f.name: f for f in dataclasses.fields(Config)}


def load_config(repo: Path) -> Config:
    """Defaults, overridden by `[tool.patchwarden]` in `<repo>/pyproject.toml` if present."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return Config()
    try:
        data = tomllib.loads(pyproject.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{pyproject}: {e}") from e
    return from_dict(data.get("tool", {}).get("patchwarden", {}))


def from_dict(raw: dict) -> Config:
    unknown = sorted(set(raw) - set(_FIELDS))
    if unknown:
        raise ConfigError(f"unknown [tool.patchwarden] keys: {', '.join(unknown)}")
    defaults = Config()
    for key, value in raw.items():
        expected = type(getattr(defaults, key))
        if isinstance(value, bool) and expected is not bool:
            raise ConfigError(f"[tool.patchwarden] {key}: expected {expected.__name__}")
        ok = isinstance(value, expected) or (
            # None-default fields take a string; int is accepted where a float is expected.
            (getattr(defaults, key) is None and isinstance(value, str))
            or (expected is float and isinstance(value, int))
        )
        if not ok:
            raise ConfigError(f"[tool.patchwarden] {key}: expected {expected.__name__}")
    for key, kind in (("models", str), ("reasoning", bool)):  # merged onto the defaults
        if key in raw:
            bad = [k for k, v in raw[key].items() if not isinstance(v, kind)]
            if bad:
                raise ConfigError(f"[tool.patchwarden] {key}.{bad[0]}: expected {kind.__name__}")
            raw = {**raw, key: {**getattr(defaults, key), **raw[key]}}
    return dataclasses.replace(defaults, **raw)
