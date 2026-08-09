# Project Layout — Canonical Structure

All agents built from this boilerplate must follow this layout exactly. The sales-agent repo (`smallTechOrg/sales-agent`) is the canonical reference.

---

## README Requirements (Mandatory)

Every generated project **must** have a README that:

1. **States "all commands run from the repo root"** — the repo root IS the project (no subdirectory to cd into). Put this as a blockquote or bold warning at the very top, before any other content.
2. **Prefixes all commands with `uv run`** — never bare `alembic`, `pytest`, or `python`. Bare commands fail unless the venv is manually activated.
3. **Includes `uv run alembic current` after `upgrade head`** — so the user can verify tables were actually created (blank output = silent failure).
4. **Stays accurate** — every README command must be tested before a phase is marked complete. If a command fails, fix the README before claiming the phase is done.

The README is the first thing a user touches. A wrong README fails the entire build regardless of whether the code works.

---

## Source Code Rule (Non-Negotiable)

**All application source code must live inside `src/`.** Never place HTML, CSS, JavaScript, Python packages, templates, or data files at the repo root.

The repo root is for project-level config only: `pyproject.toml`, `alembic.ini`, `README.md`, `.env.example`, and boilerplate infrastructure (`spec/`, `harness/`, `CLAUDE.md`). If you are about to create an application file at the root, stop and put it in `src/` instead.

This applies to all project types — Python packages, static web apps, TypeScript projects, and any other stack.

---

## Directory Tree

The repo root **is** the agent project. There is no `<agent-slug>/` subdirectory — boilerplate files (`spec/`, `harness/`, `CLAUDE.md`) coexist with project files at the root.

**One package only.** The skeleton baseline shipped `src/agent/` with a `transform_text`
capability slot. Once a phase **replaces** that slot, the baseline's dead artifacts must be
deleted so generators are never misdirected — this project's slot is the triage graph, so the
flat layout lives directly under `src/` with no `src/agent/` subpackage:

```
<repo root>                           ← repo root IS the agent project
├── src/
│   ├── __init__.py
│   ├── __main__.py                   ← `uv run python -m src` entry point
│   ├── api/                          ← FastAPI routers (app factory + routers in __init__.py)
│   │   ├── _common.py                ← ok() / api_error() response envelope
│   │   ├── auth.py                   ← Google OAuth web flow
│   │   ├── connections.py            ← /api/connections/*
│   │   ├── triage.py                 ← /api/triage/* (clusters, items, review, approve)
│   │   ├── runs.py                   ← /api/runs/*
│   │   ├── session.py                ← session cookie deps
│   │   └── _common.py
│   ├── channels/                     ← channel-adapter interface + gmail impl
│   │   ├── base.py                   ← ChannelAdapter ABC, ChannelItem, SenderSignal
│   │   └── gmail/                    ← oauth.py, store.py, adapter.py, normalize.py
│   ├── config/
│   │   └── settings.py               ← Pydantic BaseSettings (AGENT_ env prefix)
│   ├── db/
│   │   ├── models.py                 ← SQLAlchemy 2.0 declarative (flat, no subpackage)
│   │   ├── session.py                ← engine + sessionmaker + create_db_session
│   │   └── seed.py                   ← ensure_default_taxonomy
│   ├── domain/                       ← pydantic models for the API surfaces
│   ├── graph/
│   │   ├── agent.py                  ← StateGraph compiled once at startup
│   │   ├── nodes.py                  ← node functions: (state) → state
│   │   ├── edges.py                  ← conditional routing functions
│   │   ├── state.py                  ← TriageState TypedDict
│   │   ├── runner.py                 ← run_triage() entry point
│   │   └── persistence.py            ← load_context / persist_run_results / persist_sender_profiles
│   ├── llm/
│   │   ├── client.py                 ← get_llm_client() (NVIDIA NIM provider)
│   │   └── providers/                ← base.py, nvidia.py (no anthropic/gemini)
│   ├── observability/
│   │   ├── events.py                 ← structlog + LangSmith tracing
│   │   ├── logging.py
│   │   └── tracing.py
│   ├── security/
│   │   └── crypto.py                 ← Fernet token cipher
│   ├── tools/
│   │   ├── rules.py                  ← Tier 1 rules + Tier 2 sender-history matchers
│   │   ├── clustering.py             ← thread-clustering
│   │   └── redact.py                 ← snippet/PII redactor
│   └── prompts/
│       ├── classify.md               ← batch classification prompt
│       └── deep_read.md              ← single-thread deep read prompt
├── tests/                            ← tests at repo root, NOT inside src/
│   ├── conftest.py
│   ├── unit/...
│   ├── integration/...               ← test_triage_pipeline.py (220-thread gate), test_gmail_adapter.py, test_no_body_persisted.py
│   └── e2e/...                       ← smoke.spec.ts, triage.spec.ts (Playwright)
├── alembic/
├── spec/
├── harness/
├── CLAUDE.md
├── pyproject.toml
├── alembic.ini
├── .env.example
└── README.md
```

**Critical:** `tests/` is at the repo root — **not** inside `src/`. The `pyproject.toml` must have `testpaths = ["tests"]` (not `["src/tests"]`).

---

## Exact File Shapes

### alembic/script.py.mako

This file **must be created manually** — it is not generated by anything. Without it, `alembic revision --autogenerate` fails with `FileNotFoundError`.

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

### Phase 1 alembic sequence (mandatory, in order)

All commands run from the **repo root** (where `alembic.ini` and `pyproject.toml` live).

```bash
# 1. Create the alembic/ directory and files (env.py, alembic.ini, script.py.mako)
# 2. Define all SQLAlchemy models in src/<package>/db/models.py
# 3. Generate the initial migration — requires the DB to be reachable and DATABASE_URL to be set:
uv run alembic revision --autogenerate -m "initial"
# 4. Apply the migration:
uv run alembic upgrade head
# 5. Verify — this command must show the revision hash, not blank output:
uv run alembic current
```

**Phase 1 is not complete until `alembic current` shows a revision.** Blank output from `alembic current` means no migration was applied.

### config/settings.py

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",   # this project's prefix
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(...)
    # Filled from .env (the single manual user step, requested at intake) and
    # required for the real-provider gate; fail fast at startup if it is absent.
    nvidia_api_key: str = Field(default="")
    llm_model: str = Field(default="nvidia/nemotron-3-nano-30b-a3b")
    log_level: str = Field(default="INFO")

_settings: Settings | None = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
```

> This project's package is flat — modules import as `from config.settings import get_settings`, not `<package>.config.settings`. See `src/__main__.py` / `src/api/__init__.py` for the actual entry points.

### db/session.py

```python
from contextlib import contextmanager
from collections.abc import Generator
from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None

def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        from config.settings import get_settings
        _engine = create_engine(get_settings().database_url, echo=False)
    return _engine

def _get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal

def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency."""
    with _get_session_factory()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

@contextmanager
def create_db_session() -> Generator[Session, None, None]:
    """Standalone — for graph nodes, CLI, scripts."""
    with _get_session_factory()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

def init_db() -> None:
    from db.models import Base
    Base.metadata.create_all(bind=_get_engine())
```

### db/models.py

```python
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Text, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

def _uuid() -> str:
    return str(uuid4())

def _now() -> datetime:
    return datetime.now(timezone.utc)

class Base(DeclarativeBase):
    pass

class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_now, onupdate=_now)
```

### graph/state.py

```python
from typing import Annotated, TypedDict
import operator

class TriageState(TypedDict, total=False):
    run_id: str
    user_id: str
    error: str | None
    # domain fields (items, sender_stats, rules, decisions, clusters, counts, cost …)
    resolved: Annotated[list[dict], operator.add]
    llm_decisions: Annotated[list[dict], operator.add]
```

### graph/nodes.py (shape)

```python
from graph.state import TriageState

def fetch_items(state: TriageState) -> dict:
    ...
```

### graph/edges.py

```python
from graph.state import TriageState

def after_fetch(state: TriageState) -> str:
    if state.get("error"):
        return "handle_error"
    return "process"
```

### graph/agent.py

```python
from langgraph.graph import StateGraph, END
from graph.state import TriageState
from graph.nodes import fetch_items, process, handle_error, finalize

def _build_graph() -> StateGraph:
    g = StateGraph(TriageState)
    ...
    return g.compile()

triage_graph = _build_graph()
```

### graph/runner.py

```python
from graph.agent import triage_graph
from graph.state import TriageState
from graph.persistence import update_run
from db.session import create_db_session

def run_triage(*, user_id: str, channel_account_id: str, limit: int = 200, ...) -> str:
    """Creates (or resumes) a TriageRun, invokes the triage graph, returns run_id."""
    ...
    final = triage_graph.invoke(initial, config={"max_concurrency": 4, "recursion_limit": 100})
    return final["run_id"]
```

### api/__init__.py

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def _lifespan(app: FastAPI):
    from db.session import create_db_session  # engine is lazily built
    yield

def create_app() -> FastAPI:
    app = FastAPI(title="Zero Inbox Agent", version="0.1.0", lifespan=_lifespan)
    from api.triage import router as triage_router
    from api.runs import router as runs_router
    from api.connections import router as connections_router
    app.include_router(triage_router)
    app.include_router(runs_router)
    app.include_router(connections_router)
    return app

app = create_app()
```

### api/_common.py

```python
from typing import Any
from fastapi import HTTPException

def ok(data: Any) -> dict:
    return {"data": data, "error": None}

def api_error(code: str, message: str, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
```

### tests/conftest.py

This project is flat — modules import without a package prefix. The conftest resets cached settings/singleton state per test:

```python
import pytest

@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    """Reset cached settings so env patches take effect in every test."""
    import config.settings as m
    m._settings = None
    yield
    m._settings = None
```

### tests/integration/test_pipeline.py

Integration tests run end-to-end against the **real LLM/API** using keys loaded
from `.env` (via `get_settings()`), against an isolated copy of the production DB
driver. Assert on the run's structural result (status, shape, key fields), not on
exact model prose. If a required key is genuinely absent, `pytest.skip` — never
fall back to a stub key as the default path. Integration tests also cover edge
cases and error paths, not just the happy run.

Key files: `tests/integration/test_triage_pipeline.py` (the 220-thread gate),
`tests/integration/test_gmail_adapter.py`, `tests/integration/test_no_body_persisted.py`.

---

## Rules

1. **Agent code goes in `src/<package>/`** — never in the boilerplate root
2. **No repository pattern** — direct SQLAlchemy queries in graph nodes and API handlers
3. **`graph/` not `agent/`** — directory name matches sales-agent convention
4. **TypedDict state** — not dataclass or Pydantic model
5. **Tools are pure functions** — `(inputs) → domain model`, no class instantiation
6. **Prompts are `.md` files** in `<package>/prompts/` — loaded at runtime
7. **LLM abstraction** — `LLMClient` wrapper, never call provider SDK directly in nodes
8. **FastAPI response envelope** — every route returns `ok(data)` or raises `api_error()`
9. **Settings singleton** must be resettable via `monkeypatch.setattr(m, "_settings", None)`
10. **Phase 2 gate runs against real services** — tests and the golden-path smoke hit the real LLM/API using keys loaded from `.env` (requested at intake), against the production DB driver (never SQLite if production is PostgreSQL). A stub provider remains only as an optional fallback when a key is genuinely absent; offline-passing is no longer required, and real-key execution is the default and required path for the gate.
