import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No test may reach OpenRouter or write into the project: there is no API key (the
    project's .env is not loaded), and the cwd is a temp dir, so default outputs
    (patchwarden.patch, the report, .patchwarden/) land there."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.chdir(tmp_path)
