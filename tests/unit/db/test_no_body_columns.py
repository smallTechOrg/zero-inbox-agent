"""The privacy invariant: no column anywhere may hold email body text."""

import pytest

from db.models import FORBIDDEN_COLUMN_SUBSTRINGS, SNIPPET_MAX_CHARS, Base
from domain import Item


def _all_columns():
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            yield table.name, column


def test_no_body_bearing_column_exists_in_metadata():
    offenders = [
        f"{table}.{col.name}"
        for table, col in _all_columns()
        if any(bad in col.name.lower() for bad in FORBIDDEN_COLUMN_SUBSTRINGS)
    ]
    assert offenders == [], f"body-bearing columns present in schema: {offenders}"


def test_items_only_content_column_is_the_redacted_snippet():
    content_columns = {
        c.name
        for c in Base.metadata.tables["items"].columns
        if c.name in {"snippet_redacted", "subject"}
    }
    assert content_columns == {"snippet_redacted", "subject"}
    assert "snippet" not in {
        c.name for c in Base.metadata.tables["items"].columns
    }, "raw (unredacted) snippet column must not exist"


def test_decisions_table_has_reasoning_but_no_content_column():
    names = {c.name for c in Base.metadata.tables["decisions"].columns}
    assert "reasoning" in names
    assert not (names & {"body", "body_text", "message_text", "content"})


@pytest.mark.parametrize("length", [0, 200, 5000])
def test_domain_item_truncates_snippet_to_200_chars(length):
    item = Item(
        user_id="u1",
        channel_account_id="ca1",
        external_thread_id="t1",
        snippet_redacted="x" * length,
    )
    assert len(item.snippet_redacted) == min(length, SNIPPET_MAX_CHARS)
