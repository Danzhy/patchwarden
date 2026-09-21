import pytest

from patchwarden.config import Config, ConfigError, from_dict, load_config


def test_defaults_without_pyproject(tmp_path):
    assert load_config(tmp_path) == Config()


def test_override_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.patchwarden]\ntest_command = "pytest -q"\nmax_fix_rounds = 3\n'
        'budget_usd = 1\nmodels = { fixer = "x/y" }\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.test_command == "pytest -q"
    assert cfg.max_fix_rounds == 3
    assert cfg.budget_usd == 1
    # Partial models table is merged onto the defaults.
    assert cfg.models["fixer"] == "x/y"
    assert cfg.models["triage"] == Config().models["triage"]


def test_reasoning_table_is_merged():
    cfg = from_dict({"reasoning": {"fixer": True}})
    assert cfg.reasoning == {"triage": False, "fixer": True, "verifier": False}


def test_unknown_key_is_an_error():
    with pytest.raises(ConfigError, match="protected_path"):
        from_dict({"protected_path": ["x/**"]})


@pytest.mark.parametrize(
    "raw",
    [
        {"max_fix_rounds": "2"},
        {"max_fix_rounds": True},
        {"auto_fix": "ruff:F401"},
        {"models": {"fixer": 1}},
        {"reasoning": {"triage": "yes"}},
    ],
)
def test_wrong_type_is_an_error(raw):
    with pytest.raises(ConfigError):
        from_dict(raw)


def test_invalid_toml(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.patchwarden\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_config_hash_tracks_changes():
    assert Config().config_hash() == Config().config_hash()
    assert from_dict({"max_fix_rounds": 5}).config_hash() != Config().config_hash()
