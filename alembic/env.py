import sys
from pathlib import Path

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make src/ importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config.settings import get_settings
from db.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False is load-bearing, not cosmetic. The default
    # (True) permanently sets .disabled on every logger created before this
    # runs — which, when alembic is invoked IN-PROCESS (the migration tests),
    # silences every `zero_inbox.*` logger for the rest of the process. That
    # cost slice 3 four real test failures that only appeared in full-suite
    # order, and in an app whose whole transparency story is "every log line
    # reaches the user", a silent global logger kill is the worst possible
    # default.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _sync_url(url: str) -> str:
    """Alembic runs synchronously — strip any async driver qualifier.

    The stack is sync SQLAlchemy 2.0 (spec/architecture.md), but a `.env` may
    carry `sqlite+aiosqlite://`. Normalize rather than fail the migration.
    """
    return url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


def _ensure_sqlite_dir(url: str) -> None:
    if not url.startswith("sqlite"):
        return
    path = url.split("///", 1)[-1]
    if path and path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


_url = _sync_url(get_settings().database_url)
_ensure_sqlite_dir(_url)
config.set_main_option("sqlalchemy.url", _url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
