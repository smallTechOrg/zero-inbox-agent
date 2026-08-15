"""Entry point: `uv run python -m src` serves the API + the /app static export on PORT."""

import faulthandler
import os
import sys

import uvicorn

# A native abort (an OpenSSL record-layer failure inside a thread pool, a
# segfault in a C extension) kills the interpreter without printing a single
# Python frame — which is exactly why a crashed triage run left a log that just
# stopped. faulthandler installs the C-level signal handlers that dump every
# thread's stack to stderr on SIGSEGV/SIGABRT/SIGBUS/SIGILL, so the next native
# crash is diagnosable instead of silent.
faulthandler.enable()

# Modules inside `src/` import each other as top-level packages (`config.settings`,
# `api.app`). pytest gets this from `pythonpath = ["src"]`; `python -m src` does not,
# so put the package directory on sys.path before uvicorn resolves "api:app".
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _port() -> int:
    try:
        from config.settings import get_settings

        port = getattr(get_settings(), "port", None)
        if port:
            return int(port)
    except Exception:  # noqa: BLE001 — settings must never block the server from booting
        pass
    return int(os.environ.get("PORT", "8001"))


def _check_fatal_config() -> None:
    """Fail fast, readably, and *unrepeatably* on an unrecoverable config error.

    Without this the missing-key error surfaced as a bare traceback with status 1,
    which `scripts/run-server.sh` reads as an ordinary crash — so a one-line config
    mistake became a 100-restart loop. Exiting with `FatalConfigError.exit_code`
    (78 = os.EX_CONFIG) is the signal the supervisor uses to stop instead.
    """
    from config.settings import FatalConfigError, fatal_config_banner, require_secret_key

    try:
        require_secret_key()
    except FatalConfigError as exc:
        print(fatal_config_banner(str(exc)), file=sys.stderr, flush=True)
        sys.exit(exc.exit_code)


if __name__ == "__main__":
    _check_fatal_config()
    uvicorn.run("api:app", host="0.0.0.0", port=_port(), reload=False)
