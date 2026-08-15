"""AGENT_SECRET_KEY is required — a missing value is a fatal, readable startup error.

The server runs under `scripts/run-server.sh`, which restarts on any non-zero exit
up to 100 times. So the *text* of this failure is the deliverable: a bare KeyError or
a pydantic ValidationError dump would turn a one-line config mistake into an
unreadable crash loop. Every assertion below is about that.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def clean_settings(monkeypatch):
    """Reset the cached settings singleton around each test (it is process-global)."""
    import config.settings as settings_module

    settings_module._settings = None
    # `.env` is read by pydantic-settings; point it at nothing so the real file (which
    # does hold a valid key) can never leak into the missing-key cases. The real .env
    # is never read, printed or modified by this test module.
    monkeypatch.setattr(
        settings_module.Settings, "model_config", {**settings_module.Settings.model_config}
    )
    settings_module.Settings.model_config["env_file"] = None
    yield settings_module
    settings_module._settings = None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_secret_key_is_a_fatal_named_error(clean_settings, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("AGENT_SECRET_KEY", value)

    with pytest.raises(clean_settings.FatalConfigError) as excinfo:
        clean_settings.require_secret_key()

    message = str(excinfo.value)
    # The whole point: the operator is told the variable name and how to make a value.
    assert "AGENT_SECRET_KEY" in message
    assert ".env" in message
    assert 'python -c "import secrets; print(secrets.token_hex(32))"' in message
    # Not a bare KeyError, not a pydantic dump.
    assert not isinstance(excinfo.value, KeyError)
    assert "validation error" not in message.lower()
    # One actionable line, not a wall of text.
    assert "\n" not in message


def test_fatal_error_is_unmissable_and_signals_do_not_respin(clean_settings):
    banner = clean_settings.fatal_config_banner()
    lines = [line for line in banner.strip().splitlines() if line]

    assert lines[0].startswith("=")
    assert "AGENT_SECRET_KEY" in lines[1]
    assert lines[-1].startswith("=")
    # os.EX_CONFIG — a supervisor can tell "misconfigured" from "crashed".
    assert clean_settings.FatalConfigError.exit_code == 78


def test_valid_key_loads_and_is_returned_verbatim(clean_settings, monkeypatch):
    monkeypatch.setenv("AGENT_SECRET_KEY", "a-real-looking-key-0123456789abcdef")

    assert clean_settings.get_settings().secret_key == "a-real-looking-key-0123456789abcdef"
    assert clean_settings.require_secret_key() == "a-real-looking-key-0123456789abcdef"

    from security.crypto import get_secret_key

    assert get_secret_key() == "a-real-looking-key-0123456789abcdef"


def test_crypto_get_secret_key_raises_instead_of_returning_empty(
    clean_settings, monkeypatch
):
    monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)

    from security.crypto import get_secret_key

    with pytest.raises(clean_settings.FatalConfigError):
        get_secret_key()


def test_no_insecure_fallback_survives_anywhere_in_src():
    """No live code path can produce `"insecure-dev-key"` or an empty-string key.

    Docstrings that *explain* the deleted fallback are fine and deliberate; only a
    real string literal in executable position is a defect, so the check is AST-based.
    """
    import ast

    offenders = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and node.value == "insecure-dev-key"
                and id(node) not in docstrings
            ):
                offenders.append(f"{path}:{node.lineno}")

    assert offenders == []


def test_env_example_documents_the_requirement_without_a_placeholder_value():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "AGENT_SECRET_KEY=\n" in text  # no committed value, real or placeholder
    assert "AGENT_SECRET_KEY=change-me" not in text
    assert "secrets.token_hex(32)" in text
