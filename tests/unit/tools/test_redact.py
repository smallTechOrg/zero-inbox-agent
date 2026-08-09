"""Redaction is the single egress chokepoint — nothing leaves the machine unredacted."""

import pytest

from tools.redact import redact, redact_item, redact_items, redact_snippet


class TestApiKeys:
    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwx",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
            "AKIAIOSFODNN7EXAMPLE",
            "nvapi-abcDEF123456789012345",
            "xoxb-1234567890-abcdefghijk",
        ],
    )
    def test_api_keys_are_stripped(self, secret):
        out = redact(f"Here is your key: {secret} — keep it safe")
        assert secret not in out
        assert "[REDACTED:api_key]" in out

    def test_surrounding_text_survives(self):
        out = redact("Deploy failed, token sk-abcdefghijklmnopqrstuvwx rotated")
        assert out.startswith("Deploy failed, token ")
        assert out.endswith(" rotated")


class TestCardNumbers:
    def test_luhn_valid_card_is_stripped(self):
        out = redact("Card 4111 1111 1111 1111 charged $9")
        assert "4111" not in out
        assert "[REDACTED:card]" in out

    def test_non_luhn_long_number_is_kept(self):
        # An order number is not a card number; over-redacting destroys useful signal.
        assert "1234567890123" in redact("Order 1234567890123 shipped")


class TestOtps:
    @pytest.mark.parametrize(
        "text",
        [
            "Your verification code is 483920",
            "OTP: 4821",
            "Use security code 99213 to sign in",
            "Your one-time password is 774411",
        ],
    )
    def test_otp_digits_are_stripped(self, text):
        out = redact(text)
        assert "[REDACTED:otp]" in out
        assert not any(chunk.isdigit() and len(chunk) >= 4 for chunk in out.split())

    def test_plain_number_without_security_context_is_kept(self):
        assert "2024" in redact("Invoice for 2024 attached")


class TestPasswords:
    def test_labelled_password_is_stripped(self):
        out = redact("password: hunter2goes-here")
        assert "hunter2goes-here" not in out
        assert "[REDACTED:password]" in out

    def test_api_key_label_is_stripped(self):
        assert "abc123def456" not in redact("api_key = abc123def456")


class TestEdgeCases:
    def test_empty_and_none(self):
        assert redact("") == ""
        assert redact(None) == ""

    def test_clean_text_is_unchanged(self):
        text = "Hey, are we still on for Thursday?"
        assert redact(text) == text

    def test_snippet_is_truncated_to_200_chars(self):
        assert len(redact_snippet("x" * 500)) == 200

    def test_multiple_secret_kinds_in_one_string(self):
        out = redact("key sk-abcdefghijklmnopqrstuvwx code 483920 password: s3cr3tvalue")
        assert "[REDACTED:api_key]" in out
        assert "[REDACTED:otp]" in out
        assert "[REDACTED:password]" in out


class TestItems:
    def test_item_subject_and_snippet_are_redacted_and_body_dropped(self):
        item = {
            "id": "i1",
            "subject": "Your code is 483920",
            "snippet": "password: letmein1234 " + "y" * 400,
            "body": "the entire email body which must never survive",
        }
        out = redact_item(item)
        assert "483920" not in out["subject"]
        assert "letmein1234" not in out["snippet_redacted"]
        assert len(out["snippet_redacted"]) <= 200
        assert "body" not in out and "snippet" not in out

    def test_redact_items_mutates_in_place(self):
        items = [{"id": "a", "subject": "OTP: 1234", "snippet": "hi"}]
        returned = redact_items(items)
        assert returned is items
        assert "1234" not in items[0]["subject"]

    def test_missing_fields_do_not_raise(self):
        assert redact_item({"id": "x"}) == {
            "id": "x",
            "subject": "",
            "snippet_redacted": "",
        }
