"""``PATCH /api/settings`` — the autonomy slider is a real, validated instrument.

spec/capabilities/drive-to-inbox-zero.md Rule A5 + E3, and spec/data.md
§ user_settings. The old 0.95 default is gone: it sat above the model's entire
measured output range, which is exactly why the shipped slider never archived
anything.
"""

from __future__ import annotations

import pytest


def _patch(client, body: dict):
    return client.patch("/api/settings", json=body)


# --- the default ---------------------------------------------------------------


def test_a_user_with_no_settings_row_sees_the_calibrated_080_default(client, seed, sign_in, db):
    from db.models import UserSettings

    db.query(UserSettings).delete()
    db.commit()

    sign_in("user-alice")
    data = client.get("/api/me").json()["data"]
    assert data["settings"]["auto_act_threshold"] == pytest.approx(0.80)
    assert data["settings"]["confidence_floor"] == pytest.approx(0.75)


def test_creating_a_settings_row_via_patch_defaults_the_bar_to_080(client, seed, sign_in, db):
    from db.models import UserSettings

    db.query(UserSettings).delete()
    db.commit()

    sign_in("user-alice")
    res = _patch(client, {"timezone": "Europe/London"})
    assert res.status_code == 200
    assert res.json()["data"]["auto_act_threshold"] == pytest.approx(0.80)


# --- Rule A5: validation -------------------------------------------------------


@pytest.mark.parametrize("value", [0, 0.0, -0.1, -1, 1.01, 2, 100])
def test_an_out_of_range_autonomy_threshold_is_rejected(client, seed, sign_in, value):
    sign_in("user-alice")
    res = _patch(client, {"auto_act_threshold": value})
    # 422 is this project's canonical status for `validation_error` (api/_common.py).
    assert res.status_code == 422
    body = res.json()
    assert body["data"] is None
    assert body["error"]["code"] == "validation_error"


def test_a_non_numeric_autonomy_threshold_is_rejected(client, seed, sign_in):
    sign_in("user-alice")
    res = _patch(client, {"auto_act_threshold": "high"})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"


def test_a_rejected_patch_does_not_persist_anything(client, seed, sign_in):
    sign_in("user-alice")
    before = client.get("/api/me").json()["data"]["settings"]["auto_act_threshold"]
    _patch(client, {"auto_act_threshold": 0, "timezone": "Mars/Olympus"})
    after = client.get("/api/me").json()["data"]["settings"]
    assert after["auto_act_threshold"] == pytest.approx(before)
    assert after["timezone"] != "Mars/Olympus"


@pytest.mark.parametrize("value", [0.01, 0.75, 0.8, 0.85, 0.9, 1.0])
def test_an_in_range_autonomy_threshold_is_accepted_and_persisted(client, seed, sign_in, value):
    sign_in("user-alice")
    res = _patch(client, {"auto_act_threshold": value})
    assert res.status_code == 200
    assert res.json()["data"]["auto_act_threshold"] == pytest.approx(value)
    assert client.get("/api/me").json()["data"]["settings"][
        "auto_act_threshold"
    ] == pytest.approx(value)


def test_the_confidence_floor_is_validated_the_same_way(client, seed, sign_in):
    sign_in("user-alice")
    assert _patch(client, {"confidence_floor": 0}).status_code == 422
    assert _patch(client, {"confidence_floor": 1.5}).status_code == 422
    assert _patch(client, {"confidence_floor": 0.7}).status_code == 200


# --- Rule A5 / E3: the above-model-ceiling warning -----------------------------


def test_a_threshold_above_090_is_accepted_but_carries_the_ceiling_warning(client, seed, sign_in):
    sign_in("user-alice")
    res = _patch(client, {"auto_act_threshold": 0.93})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["auto_act_threshold"] == pytest.approx(0.93)
    assert data["warning"] == "above_model_ceiling"


def test_the_old_095_default_now_warns_rather_than_silently_doing_nothing(client, seed, sign_in):
    # The exact value that shipped as the default and archived zero threads.
    sign_in("user-alice")
    assert _patch(client, {"auto_act_threshold": 0.95}).json()["data"]["warning"] == (
        "above_model_ceiling"
    )


@pytest.mark.parametrize("value", [0.80, 0.85, 0.90])
def test_a_threshold_at_or_below_090_carries_no_warning(client, seed, sign_in, value):
    sign_in("user-alice")
    assert "warning" not in _patch(client, {"auto_act_threshold": value}).json()["data"]


def test_a_patch_that_does_not_touch_the_threshold_carries_no_warning(client, seed, sign_in):
    sign_in("user-alice")
    assert "warning" not in _patch(client, {"timezone": "UTC"}).json()["data"]


# --- isolation -----------------------------------------------------------------


def test_a_patch_never_touches_another_users_settings(client, seed, sign_in, db):
    from db.models import UserSettings

    sign_in("user-alice")
    _patch(client, {"auto_act_threshold": 0.99})
    db.expire_all()
    assert db.get(UserSettings, "user-bob").auto_act_threshold == pytest.approx(0.80)


def test_an_unauthenticated_patch_is_rejected(client, seed):
    assert _patch(client, {"auto_act_threshold": 0.85}).status_code == 401
