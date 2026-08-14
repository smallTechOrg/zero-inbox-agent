"""Migration ``0006_autonomy_policy`` against a database holding REAL-SHAPED rows.

spec/data.md § Phase 7 migration. The production database holds 11,449 decisions
for two real accounts, one at ``auto_act_threshold = 0.95`` (the unused shipped
default, above the model's entire measured range) and one at ``0.75`` (a
deliberate "act on everything above the floor" setting). This test builds exactly
that shape against an **isolated** file database — never the real one — and
asserts the migration does the right thing to each of them.

The migration is run stepwise (``0005`` → seed rows → ``0006``) so the seeded rows
genuinely pre-date the upgrade, which is the only way the data statements are
actually exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def stepwise(tmp_path, monkeypatch):
    """An isolated DB upgraded to 0005 (i.e. the state at the end of Phase 6)."""
    db_path = tmp_path / "phase7.db"
    monkeypatch.setenv("AGENT_DATABASE_URL", f"sqlite:///{db_path}")
    import config.settings as settings_module

    settings_module._settings = None

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "0005_decision_review_state")

    engine = create_engine(f"sqlite:///{db_path}")
    yield cfg, engine
    engine.dispose()


def _seed_pre_phase7_rows(engine) -> None:
    """Two real accounts (0.95 and 0.75), the six default categories, and one
    completed run with decisions — all written BEFORE 0006 runs."""
    with engine.begin() as conn:
        for tag, threshold in (("alice", 0.95), ("bob", 0.75)):
            conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, created_at) "
                    "VALUES (:id, :email, :name, '2026-01-01T00:00:00')"
                ),
                {"id": f"user-{tag}", "email": f"{tag}@x.com", "name": tag},
            )
            conn.execute(
                text(
                    "INSERT INTO user_settings (user_id, auto_act_threshold, "
                    "confidence_floor, dry_run, llm_model, digest_hour_local, "
                    "timezone, updated_at) VALUES (:u, :t, 0.75, 0, 'm', 8, 'UTC', "
                    "'2026-01-01T00:00:00')"
                ),
                {"u": f"user-{tag}", "t": threshold},
            )
            for order, (key, name, action) in enumerate(
                [
                    ("newsletters", "Newsletters", "archive"),
                    ("notifications", "Notifications", "archive"),
                    ("receipts", "Receipts", "keep"),      # untouched seeded value
                    ("receipts_custom", "ReceiptsCustom", "digest"),
                    ("outreach", "Outreach", "archive"),
                    ("people", "People", "keep"),
                    ("urgent", "Urgent", "keep"),
                ]
            ):
                conn.execute(
                    text(
                        "INSERT INTO categories (id, user_id, key, name, description, "
                        "channel_label_name, default_action, is_default, sort_order) "
                        "VALUES (:id, :u, :k, :n, '', :l, :a, 1, :o)"
                    ),
                    {"id": f"cat-{tag}-{key}", "u": f"user-{tag}", "k": key, "n": name,
                     "l": f"ZeroInbox/{name}", "a": action, "o": order},
                )

        conn.execute(
            text(
                "INSERT INTO channel_accounts (id, user_id, channel, account_email, "
                "refresh_token_enc, scopes, status, connected_at) VALUES "
                "('conn-a', 'user-alice', 'gmail', 'a@g.com', 'ENC', '[]', "
                "'connected', '2026-01-01T00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO triage_runs (id, user_id, channel_account_id, kind, status, "
                "dry_run, items_total, items_decided, counts, tokens_in, tokens_out, "
                "cost_usd, started_at) VALUES ('run-old', 'user-alice', 'conn-a', "
                "'backlog', 'completed', 0, 3, 3, '{}', 0, 0, 0.0, "
                "'2026-01-01T00:00:00')"
            )
        )
        for n in range(3):
            conn.execute(
                text(
                    "INSERT INTO items (id, user_id, channel_account_id, "
                    "external_thread_id, from_email, from_name, from_domain, subject, "
                    "snippet_redacted, internal_date, message_count, has_attachments, "
                    "is_unread, created_at) VALUES (:id, 'user-alice', 'conn-a', :t, "
                    "'n@s.com', 'N', 's.com', 'sub', 'snip', '2026-01-01T00:00:00', 1, "
                    "0, 1, '2026-01-01T00:00:00')"
                ),
                {"id": f"item-{n}", "t": f"t{n}"},
            )
            conn.execute(
                text(
                    "INSERT INTO decisions (id, user_id, item_id, run_id, proposed_action, "
                    "confidence, reasoning, decided_by, time_sensitive, status, "
                    "review_state, created_at) VALUES (:id, 'user-alice', :item, "
                    "'run-old', 'archive', 0.85, 'r', 'llm', 0, 'proposed', "
                    "'reviewed', '2026-01-01T00:00:00')"
                ),
                {"id": f"dec-{n}", "item": f"item-{n}"},
            )


def _upgrade_head(cfg) -> None:
    command.upgrade(cfg, "head")


# --- the load-bearing data statement (step 5) --------------------------------


def test_the_095_row_becomes_080_and_the_075_row_is_untouched(stepwise):
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    _upgrade_head(cfg)

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("SELECT user_id, auto_act_threshold FROM user_settings")
            ).all()
        )
    assert rows["user-alice"] == pytest.approx(0.80)
    assert rows["user-bob"] == pytest.approx(0.75)


def test_no_user_settings_row_is_left_above_090(stepwise):
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    _upgrade_head(cfg)

    with engine.connect() as conn:
        offenders = conn.execute(
            text("SELECT COUNT(*) FROM user_settings WHERE auto_act_threshold > 0.90")
        ).scalar_one()
    assert offenders == 0


def test_a_new_user_settings_row_defaults_to_080(stepwise):
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    _upgrade_head(cfg)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, created_at) VALUES "
                "('user-new', 'new@x.com', 'New', '2026-02-01T00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO user_settings (user_id, confidence_floor, dry_run, "
                "llm_model, digest_hour_local, timezone, updated_at) VALUES "
                "('user-new', 0.75, 0, 'm', 8, 'UTC', '2026-02-01T00:00:00')"
            )
        )
        value = conn.execute(
            text("SELECT auto_act_threshold FROM user_settings WHERE user_id='user-new'")
        ).scalar_one()
    assert value == pytest.approx(0.80)


# --- steps 1 + 2: categories.auto_act_threshold ------------------------------


def test_outreach_and_receipts_are_seeded_at_085_and_the_rest_stay_null(stepwise):
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    _upgrade_head(cfg)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT key, auto_act_threshold FROM categories")
        ).all()

    by_key: dict[str, list] = {}
    for key, value in rows:
        by_key.setdefault(key, []).append(value)

    for key in ("outreach", "receipts"):
        assert all(v == pytest.approx(0.85) for v in by_key[key]), key
    for key in ("newsletters", "notifications", "people", "urgent"):
        assert all(v is None for v in by_key[key]), key


def test_a_seeded_receipts_keep_is_flipped_to_archive(stepwise):
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT DISTINCT default_action FROM categories WHERE key='receipts'")
        ).scalars().all()
    assert before == ["keep"]

    _upgrade_head(cfg)

    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT DISTINCT default_action FROM categories WHERE key='receipts'")
        ).scalars().all()
    assert after == ["archive"]


def test_a_users_own_receipts_choice_is_never_overwritten(stepwise):
    # The `AND default_action = 'keep'` guard: a category the user deliberately
    # set to `digest` keeps that value through the migration.
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE categories SET default_action='digest' "
                 "WHERE key='receipts' AND user_id='user-bob'")
        )
    _upgrade_head(cfg)

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("SELECT user_id, default_action FROM categories WHERE key='receipts'")
            ).all()
        )
    assert rows["user-bob"] == "digest"
    assert rows["user-alice"] == "archive"


def test_downgrade_returns_receipts_to_keep(stepwise):
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    _upgrade_head(cfg)
    command.downgrade(cfg, "0005_decision_review_state")

    with engine.connect() as conn:
        actions = conn.execute(
            text("SELECT DISTINCT default_action FROM categories WHERE key='receipts'")
        ).scalars().all()
    assert actions == ["keep"]


# --- steps 3 + 4: decisions.autonomy_state -----------------------------------


def test_pre_existing_decisions_are_left_with_a_null_autonomy_state(stepwise):
    # No backfill: a decision made under a policy that did not exist gets no
    # invented state. The remainder ledger reports these as `unclassified`.
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    _upgrade_head(cfg)

    with engine.connect() as conn:
        total, nulls = conn.execute(
            text(
                "SELECT COUNT(*), SUM(CASE WHEN autonomy_state IS NULL THEN 1 ELSE 0 END) "
                "FROM decisions"
            )
        ).one()
    assert total == 3
    assert nulls == 3


def test_the_remainder_ledger_index_exists(stepwise):
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    _upgrade_head(cfg)

    indexes = {ix["name"] for ix in inspect(engine).get_indexes("decisions")}
    assert "ix_decisions_run_autonomy" in indexes


# --- reversibility ------------------------------------------------------------


def test_downgrade_then_upgrade_again_is_clean(stepwise):
    cfg, engine = stepwise
    _seed_pre_phase7_rows(engine)
    _upgrade_head(cfg)

    command.downgrade(cfg, "0005_decision_review_state")
    with engine.connect() as conn:
        decision_columns = {c["name"] for c in inspect(conn).get_columns("decisions")}
        category_columns = {c["name"] for c in inspect(conn).get_columns("categories")}
    assert "autonomy_state" not in decision_columns
    assert "auto_act_threshold" not in category_columns

    # Step 5 is a deliberate ONE-WAY data migration: the downgrade does not
    # restore alice's 0.95, and the docstring of 0006 says so explicitly.
    with engine.connect() as conn:
        alice = conn.execute(
            text("SELECT auto_act_threshold FROM user_settings WHERE user_id='user-alice'")
        ).scalar_one()
    assert alice == pytest.approx(0.80)

    _upgrade_head(cfg)
    with engine.connect() as conn:
        decision_columns = {c["name"] for c in inspect(conn).get_columns("decisions")}
    assert "autonomy_state" in decision_columns


def test_upgrade_head_from_empty_still_matches_the_orm_metadata(tmp_path, monkeypatch):
    """The whole chain, from nothing — the shape the migration test suite guards."""
    from db.models import Base

    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("AGENT_DATABASE_URL", f"sqlite:///{db_path}")
    import config.settings as settings_module

    settings_module._settings = None

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        for table in ("categories", "decisions", "user_settings"):
            expected = {c.name for c in Base.metadata.tables[table].columns}
            actual = {c["name"] for c in inspector.get_columns(table)}
            assert actual == expected, f"{table} drifted: {expected ^ actual}"
    finally:
        engine.dispose()
