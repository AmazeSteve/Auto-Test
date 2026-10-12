"""Effect restore material preservation and rollback resolution; no network."""

from copy import deepcopy

import pytest

from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_restore import EffectRestoreResolver
from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.client import ApiClient
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.utils.test_data_factory import TestDataFactory as _TestDataFactory


@pytest.fixture(autouse=True)
def reset_context():
    Context.clear()
    yield
    Context.clear()


def envelope():
    return {"target": "json", "payload": {"name": "private baseline", "relation": ["one"]}}


@pytest.mark.parametrize("state", ["unknown", "applied", "not_applied"])
def test_mark_effect_stores_deepcopy_and_preserves_on_replacement(state):
    material = envelope()
    expected = deepcopy(material)
    Context.mark_effect("WRITE", state=state, restore_payload=material)
    assert Context.get_effect("WRITE")["restore_payload"] == expected
    material["payload"]["relation"].append("external mutation")
    assert Context.get_effect("WRITE")["restore_payload"] == expected
    for later_state in ("unknown", "applied", "not_applied"):
        old_record = Context.get_effect("WRITE")
        Context.mark_effect("WRITE", state=later_state)
        assert Context.get_effect("WRITE")["restore_payload"] == expected
        old_record["restore_payload"]["payload"]["name"] = "old record changed"
        assert Context.get_effect("WRITE")["restore_payload"] == expected


@pytest.mark.parametrize("operation,state", [
    (Context.mark_effect_restored, "restored"),
    (Context.mark_effect_restore_failed, "restore_failed"),
])
def test_terminal_markers_preserve_restore_material(operation, state):
    Context.mark_effect("WRITE", restore_payload=envelope())
    operation("WRITE", rollback_case_id="ROLLBACK")
    assert Context.get_effect_state("WRITE") == state
    assert Context.get_effect("WRITE")["restore_payload"] == envelope()


def test_explicit_none_is_distinct_from_omitted_restore_argument():
    Context.mark_effect("WRITE", restore_payload=envelope())
    Context.mark_effect("WRITE", restore_payload=None)
    assert Context.get_effect("WRITE")["restore_payload"] is None
    Context.mark_effect("PLAIN")
    assert "restore_payload" not in Context.get_effect("PLAIN")


def test_restore_uses_normalized_material_not_snapshot_and_copies_case():
    material = envelope()
    Context.mark_effect("WRITE", snapshot={"before": {"name": "wrong raw value"}}, restore_payload=material)
    case = {"id": "ROLLBACK", "restore_payload": {"source_effect": "WRITE"}, "params": {"a": [1]}}
    original = deepcopy(case)
    runtime, evidence = EffectRestoreResolver.apply(case)
    assert runtime["json"] == material["payload"]
    assert evidence == {"source_effect": "WRITE", "target": "json", "field_count": 2, "keys": ["name", "relation"]}
    assert "private baseline" not in str(evidence)
    runtime["json"]["relation"].append("changed")
    runtime["params"]["a"].append(2)
    assert case == original
    assert Context.get_effect("WRITE")["restore_payload"] == material


@pytest.mark.parametrize("effect", [
    None, {}, {"state": "unknown"},
    {"restore_payload": None},
    {"restore_payload": {"target": "json"}},
    {"restore_payload": {"target": "headers", "payload": {}}},
    {"restore_payload": {"target": [], "payload": {}}},
    {"restore_payload": {"payload": {}}},
    {"restore_payload": {"target": "json", "payload": None}},
    {"restore_payload": {"target": "json", "payload": []}},
])
def test_restore_requires_existing_effect_and_valid_envelope(effect):
    if effect is not None:
        Context._data["effects"]["WRITE"] = effect
    with pytest.raises(ValueError):
        EffectRestoreResolver.apply({"restore_payload": {"source_effect": "WRITE"}})


def test_restore_payload_and_mapping_cannot_coexist_at_runtime():
    with pytest.raises(ValueError, match="coexist"):
        EffectRestoreResolver.apply({"restore_payload": {"source_effect": "WRITE"}, "restore_mapping": {}})


@pytest.mark.parametrize("use_api_client", [False, True], ids=["fake-client", "api-client-fake-transport"])
def test_literal_restore_survives_resolver_runner_and_transport(monkeypatch, use_api_client):
    payload = {
        "usr_nickname": "{{literal_user_text}}",
        "usr_extra_2": "$timestamp_ms",
        "usr_email": "$data.unique_display_name",
        "nested": {"items": ["{{literal_user_text}}", "$timestamp_ms", "$data.unique_display_name"]},
    }
    material = {"target": "json", "payload": deepcopy(payload)}
    Context.mark_effect("WRITE", restore_payload=material)
    original_effect = deepcopy(Context.get_effect("WRITE"))
    Context.set_variable("route", "resolved-route")
    calls = []

    def unexpected_resolution(*args, **kwargs):
        raise AssertionError("literal restore must not generate or resolve values")

    monkeypatch.setattr(_TestDataFactory, "resolve", unexpected_resolution)
    monkeypatch.setattr(VariableResolver, "timestamp_ms", unexpected_resolution)

    class FakeResponse:
        status_code = 200
        headers = {}
        text = "offline fake response"

        @staticmethod
        def json():
            return {"code": 200}

    def request(**kwargs):
        calls.append(deepcopy(kwargs))
        assert kwargs["json"] == payload
        # Even transport-side mutation must not change Context or the runtime Case.
        kwargs["json"]["nested"]["items"].append("transport mutation")
        return FakeResponse()

    if use_api_client:
        client = ApiClient("https://example.invalid", verify_ssl=True)
        monkeypatch.setattr(client.session, "request", request)
    else:
        class FakeClient:
            @staticmethod
            def request(**kwargs):
                return request(**kwargs)
        client = FakeClient()

    case = {
        "id": "ROLLBACK", "method": "POST", "path": "/offline/{{route}}",
        "restore_payload": {"source_effect": "WRITE"},
        "validate": [{"type": "status_code", "expected": 200}],
    }
    runtime, _ = EffectRestoreResolver.apply(case)
    CaseRunner.run(runtime, lambda host: client, "offline cookie")
    assert len(calls) == 1 and calls[0]["json"] == payload
    assert Context.get_effect("WRITE") == original_effect
    assert material["payload"] == payload
    assert runtime["json"] == payload
    assert not Context.has_variable("literal_user_text")
    assert not Context.has_variable("data.unique_display_name")


@pytest.mark.parametrize("use_api_client", [False, True], ids=["fake-client", "api-client-fake-transport"])
def test_plain_yaml_json_still_resolves_all_template_forms(monkeypatch, use_api_client):
    Context.set_variable("literal_user_text", "resolved text")
    data_calls = []

    def provide(key):
        data_calls.append(key)
        return "generated display name"

    monkeypatch.setattr(_TestDataFactory, "resolve", provide)
    monkeypatch.setattr(VariableResolver, "timestamp_ms", lambda: "1700000000000")
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"code": 200}

    def request(**kwargs):
        calls.append(deepcopy(kwargs))
        return FakeResponse()

    if use_api_client:
        client = ApiClient("https://example.invalid", verify_ssl=True)
        monkeypatch.setattr(client.session, "request", request)
    else:
        class FakeClient:
            @staticmethod
            def request(**kwargs):
                return request(**kwargs)
        client = FakeClient()
    body = {"usr_nickname": "{{literal_user_text}}", "usr_extra_2": "$timestamp_ms", "usr_email": "$data.unique_display_name"}
    CaseRunner.run({"id": "PLAIN", "method": "POST", "path": "/offline", "json": body}, lambda host: client, "offline cookie")
    assert calls[0]["json"] == {
        "usr_nickname": "resolved text", "usr_extra_2": "1700000000000", "usr_email": "generated display name",
    }
    assert data_calls == ["unique_display_name"]
    assert body["usr_nickname"] == "{{literal_user_text}}"
    assert body["usr_extra_2"] == "$timestamp_ms"
    assert body["usr_email"] == "$data.unique_display_name"


@pytest.mark.parametrize("target", ["json", "data", "params", "headers", "files", "other"])
def test_b2_restore_envelope_accepts_json_only(target):
    material = envelope()
    material["target"] = target
    Context.mark_effect("WRITE", restore_payload=material)
    case = {"restore_payload": {"source_effect": "WRITE"}}
    if target == "json":
        runtime, _ = EffectRestoreResolver.apply(case)
        assert runtime["json"] == material["payload"]
    else:
        with pytest.raises(ValueError, match="target must be json"):
            EffectRestoreResolver.apply(case)


@pytest.mark.parametrize("target", ["json", "data", "params"])
def test_legacy_restore_mapping_retains_all_supported_targets(target):
    Context.mark_effect("WRITE", snapshot={"before": {"field": "original value"}})
    case = {"restore_mapping": {"source_effect": "WRITE", "fields": {target + ".field": "before.field"}}}
    runtime, _ = EffectRestoreResolver.apply(case)
    assert runtime[target] == {"field": "original value"}
    assert target not in case
