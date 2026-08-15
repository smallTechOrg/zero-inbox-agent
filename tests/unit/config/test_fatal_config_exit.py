"""A fatal config error must STOP the supervisor loop, not feed it.

Follow-up A made `AGENT_SECRET_KEY` required and gave the failure a readable banner.
But a readable banner printed 100 times is still a crash loop: `scripts/run-server.sh`
restarts on any non-zero exit up to MAX_RESTARTS with a 2s backoff. These tests pin the
two halves of the contract that make the loop actually stop:

  1. `python -m src` converts `FatalConfigError` into banner-on-stderr + exit 78.
  2. `run-server.sh` treats status 78 as unrecoverable configuration and exits, while
     still restarting on an ordinary crash and still not restarting a clean exit.

The supervisor is exercised with a stub command — no server is started, no port bound.
The real `.env` is never read, printed or modified here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Imported at module scope on purpose: `src/api/__init__.py` builds `app = create_app()`
# at import time, so importing it *inside* a test that has already unset the key would
# blow up in the import statement rather than at the call site under test.
import api as api_module  # noqa: E402
from api import session as session_module  # noqa: E402
from security.crypto import get_secret_key  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SERVER = REPO_ROOT / "scripts" / "run-server.sh"


# --------------------------------------------------------------------------- #
# 1. The exception carries the exit code, and both unified call sites use the
#    one canonical message.
# --------------------------------------------------------------------------- #


@pytest.fixture
def clean_settings(monkeypatch):
    import config.settings as settings_module

    settings_module._settings = None
    monkeypatch.setattr(
        settings_module.Settings, "model_config", {**settings_module.Settings.model_config}
    )
    settings_module.Settings.model_config["env_file"] = None
    yield settings_module
    settings_module._settings = None


def test_fatal_config_error_carries_os_ex_config(clean_settings):
    exc = clean_settings.FatalConfigError(clean_settings.MISSING_SECRET_KEY_MESSAGE)

    assert exc.exit_code == 78 == os.EX_CONFIG
    assert isinstance(exc, RuntimeError)


def test_banner_names_the_variable_and_the_generation_command(clean_settings):
    banner = clean_settings.fatal_config_banner()

    assert "AGENT_SECRET_KEY" in banner
    assert 'python -c "import secrets; print(secrets.token_hex(32))"' in banner


def test_session_signing_raises_the_canonical_message(clean_settings, monkeypatch):
    """`api.session` must not invent its own wording for a missing key."""
    monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)

    with pytest.raises(clean_settings.FatalConfigError) as excinfo:
        session_module._secret_key()

    assert str(excinfo.value) == clean_settings.MISSING_SECRET_KEY_MESSAGE


def test_app_startup_raises_the_canonical_message(clean_settings, monkeypatch):
    """`create_app()`'s guard must not invent its own wording either."""
    monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)

    with pytest.raises(clean_settings.FatalConfigError) as excinfo:
        api_module._require_secret_key()

    assert str(excinfo.value) == clean_settings.MISSING_SECRET_KEY_MESSAGE


def test_both_call_sites_agree_with_crypto(clean_settings, monkeypatch):
    """All three doors report identically — one message, one exit code."""
    monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)

    messages = set()
    for call in (session_module._secret_key, api_module._require_secret_key, get_secret_key):
        with pytest.raises(clean_settings.FatalConfigError) as excinfo:
            call()
        messages.add(str(excinfo.value))
        assert excinfo.value.exit_code == 78

    assert messages == {clean_settings.MISSING_SECRET_KEY_MESSAGE}


# --------------------------------------------------------------------------- #
# 2. `python -m src` exits 78 with the banner instead of a traceback.
#    Run in a subprocess with a scrubbed env and env_file disabled, so the real
#    .env cannot supply a key. No server is ever reached: the guard runs first.
# --------------------------------------------------------------------------- #


def _run_module_without_key(cwd: Path) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"AGENT_SECRET_KEY"} and not k.startswith("PYTEST")
    }
    # `env_file=".env"` is resolved relative to the working directory, so running
    # from a scratch directory guarantees the real .env is never opened, let alone
    # read or modified. PYTHONPATH still makes the `src` package importable.
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "src"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_python_m_src_exits_78_with_a_banner_and_no_traceback(tmp_path):
    result = _run_module_without_key(tmp_path)

    assert result.returncode == 78, result.stderr[-2000:]
    assert "AGENT_SECRET_KEY" in result.stderr
    assert 'secrets.token_hex(32)' in result.stderr
    assert "====" in result.stderr  # the banner rule, i.e. unmissable
    assert "Traceback (most recent call last)" not in result.stderr
    # It never got as far as binding a port.
    assert "Uvicorn running" not in (result.stdout + result.stderr)


# --------------------------------------------------------------------------- #
# 3. The supervisor stops on 78 — exercised with a stub command, never a server.
# --------------------------------------------------------------------------- #


def _supervise(exit_code: int, tmp_path: Path, max_restarts: str = "3"):
    """Run run-server.sh with the server command stubbed out by a fake `uv` on PATH."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    stub = fake_bin / "uv"
    stub.write_text(f'#!/usr/bin/env bash\nexit {exit_code}\n', encoding="utf-8")
    stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["MAX_RESTARTS"] = max_restarts
    env["BACKOFF_SECONDS"] = "0"
    return subprocess.run(
        ["bash", str(RUN_SERVER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_supervisor_does_not_restart_on_exit_78(tmp_path):
    result = _supervise(78, tmp_path)

    assert result.returncode == 78
    combined = result.stdout + result.stderr
    assert "not restarting" in combined
    assert "restart #1" not in combined  # it never came back around


def test_supervisor_still_restarts_an_ordinary_crash(tmp_path):
    result = _supervise(1, tmp_path)

    combined = result.stdout + result.stderr
    assert "restart #1" in combined  # backoff-and-retry survives
    assert "giving up after 3 restarts" in combined
    assert result.returncode == 1


def test_supervisor_still_does_not_restart_a_clean_exit(tmp_path):
    result = _supervise(0, tmp_path)

    assert result.returncode == 0
    assert "restart #1" not in (result.stdout + result.stderr)
