"""Mocked B2 CaseRunner failure paths and four-phase lifecycle integration."""

from copy import deepcopy

import pytest
import requests

from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner, LifecycleCleanupError
from wizbank_api_test.services.request_payload_resolver import RequestPayloadResolver
from wizbank_api_test.tests.unit.test_request_payload_resolver import builder_config, read_model


class Response:
    status_code = 200
    headers = {}
    text = "offline fake response"

    def __init__(self, payload=None):
        self.payload = {"code": 200} if payload is None else payload

    def json(self):
        return self.payload


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    Context.clear()

    def reject_network(*args, **kwargs):
        raise AssertionError("real HTTP is forbidden in B2 unit tests")

    monkeypatch.setattr(requests.Session, "request", reject_network)
    yield
    Context.clear()


def root_case():
    return {
        "id": "WRITE", "name": "Offline update", "method": "POST", "path": "/offline-update",
        "request_builder": builder_config(),
        "effect": {
            "target": {"fields": ["usr_display_bil"]},
            "capture": {
                "enabled": True,
                "fields": {"usr_display_bil": "$.data.usr_display_bil"},
                "before": {"endpoint": "read"}, "after": {"endpoint": "read"},
            },
        },
        "validate": [{"type": "status_code", "expected": 200}],
    }


class Client:
    def __init__(self, events, error=None, response=None):
        self.events = events
        self.error = error
        self.response = response or Response()
        self.calls = []

    def request(self, **kwargs):
        self.events.append("http")
        assert not Context.has_effect("WRITE")  # no prepared Effect before HTTP
        self.calls.append(deepcopy(kwargs))
        if self.error:
            raise self.error
        return self.response


def captures(monkeypatch, events, before_error=None, after_error=None, after_name="Test User Mutation"):
    def before(**kwargs):
        events.append("before")
        if before_error:
            raise before_error
        return {"code": 200, "data": read_model()}

    def after(**kwargs):
        events.append("after")
        if after_error:
            raise after_error
        model = read_model()
        model["usr_display_bil"] = after_name
        return {"code": 200, "data": model}

    monkeypatch.setattr(EffectExecutor, "capture_before", before)
    monkeypatch.setattr(EffectExecutor, "capture_after", after)


@pytest.mark.parametrize("failure", ["before", "source", "builder", "disabled"])
def test_pre_request_failures_do_not_send_mutation_or_create_cleanup(monkeypatch, failure):
    events = []
    resolve = RequestPayloadResolver.resolve

    def tracked_resolve(*args):
        events.append("builder")
        return resolve(*args)

    monkeypatch.setattr(RequestPayloadResolver, "resolve", tracked_resolve)
    client = Client(events)
    captures(monkeypatch, events, before_error=ValueError("before failed") if failure == "before" else None)
    case = root_case()
    if failure == "source":
        case["request_builder"]["source"]["expression"] = "$.missing"
    if failure == "builder":
        case["request_builder"]["overrides"]["usr_display_bil"] = " "
    if failure == "disabled":
        case["effect"]["capture"]["enabled"] = False
    with pytest.raises(ValueError):
        CaseRunner.run(case, lambda host: client, "fake cookie")
    assert client.calls == []
    assert not Context.has_effect("WRITE")
    assert not Context.needs_cleanup("WRITE")
    assert "after" not in events
    assert ("builder" in events) is (failure in ("source", "builder"))


def test_builder_runtime_order_records_effect_only_after_capture(monkeypatch):
    events = []
    captures(monkeypatch, events)
    resolve, mark = RequestPayloadResolver.resolve, Context.mark_effect

    def tracked_resolve(*args):
        events.append("builder")
        assert not Context.has_effect("WRITE")
        return resolve(*args)

    def tracked_mark(*args, **kwargs):
        events.append("mark")
        assert kwargs["restore_payload"]["target"] == "json"
        return mark(*args, **kwargs)

    monkeypatch.setattr(RequestPayloadResolver, "resolve", tracked_resolve)
    monkeypatch.setattr(Context, "mark_effect", tracked_mark)
    CaseRunner.run(root_case(), lambda host: Client(events), "fake cookie")
    assert events == ["before", "builder", "http", "after", "mark"]


def test_builder_mutation_literal_values_survive_api_client_transport(monkeypatch):
    from dataclasses import replace
    from wizbank_api_test.core.client import ApiClient
    from wizbank_api_test.core.variable_resolver import VariableResolver
    from wizbank_api_test.services.builder_registry import BuilderRegistry
    from wizbank_api_test.utils.test_data_factory import TestDataFactory

    model = read_model()
    literals = {
        "usr_nickname": "{{literal_user_text}}",
        "usr_extra_2": "$timestamp_ms",
        "usr_email": "$data.literal_real_value",
    }
    model.update(literals)
    before = {"code": 200, "data": model}
    original_before = deepcopy(before)
    generated = "Generated DSL Display Name"
    case = root_case()
    case["request_builder"]["overrides"]["usr_display_bil"] = "{{$data.unique_display_name}}"
    original_case = deepcopy(case)
    registration = BuilderRegistry.get("user_update")
    builder_finished = False
    plans, data_calls, transport_calls = [], [], []

    def provide(key):
        assert not builder_finished, "data factory must not run after Builder completion"
        assert key == "unique_display_name", "Read Model literals must not be resolved"
        data_calls.append(key)
        return generated

    def unexpected_timestamp():
        raise AssertionError("Read Model timestamp text must remain literal")

    def build(read_model, **kwargs):
        nonlocal builder_finished
        assert kwargs["overrides"] == {"usr_display_bil": generated}
        plan = registration.build(read_model, **kwargs)
        plans.append(plan)
        builder_finished = True
        return plan

    def transport(**kwargs):
        assert builder_finished
        body = kwargs["json"]
        assert len(body) == 27
        assert body["usr_display_bil"] == generated
        assert {key: body[key] for key in literals} == literals
        transport_calls.append(deepcopy(kwargs))
        # The actual transport receives a copy, not the frozen Builder plan.
        body["usr_nickname"] = "transport-side mutation"
        return Response()

    def after(**kwargs):
        payload = deepcopy(before)
        payload["data"]["usr_display_bil"] = generated
        return payload

    monkeypatch.setattr(TestDataFactory, "resolve", provide)
    monkeypatch.setattr(VariableResolver, "timestamp_ms", unexpected_timestamp)
    monkeypatch.setitem(BuilderRegistry._builders, "user_update", replace(registration, build=build))
    monkeypatch.setattr(EffectExecutor, "capture_before", lambda **kwargs: before)
    monkeypatch.setattr(EffectExecutor, "capture_after", after)
    client = ApiClient("https://example.invalid", verify_ssl=True)
    monkeypatch.setattr(client.session, "request", transport)

    CaseRunner.run(case, lambda host: client, "offline cookie")

    assert data_calls == ["unique_display_name"]
    assert len(transport_calls) == len(plans) == 1
    sent = transport_calls[0]["json"]
    baseline = Context.get_effect("WRITE")["restore_payload"]["payload"]
    assert len(sent) == len(baseline) == 27
    assert {key for key in sent if sent[key] != baseline[key]} == {"usr_display_bil"}
    assert {key: baseline[key] for key in literals} == literals
    assert baseline == plans[0].baseline_payload
    assert sent == plans[0].mutation_payload
    assert before == original_before
    assert case == original_case
    assert Context.get_effect_state("WRITE") == "applied"
    assert not Context.has_variable("literal_user_text")
    assert not Context.has_variable("data.literal_real_value")


@pytest.mark.parametrize("error", [requests.Timeout("offline timeout"), requests.ConnectionError("offline connection failure")])
def test_transport_failure_records_unknown_with_normalized_restore(monkeypatch, error):
    events = []
    client = Client(events, error=error)
    captures(monkeypatch, events)
    with pytest.raises(type(error)) as raised:
        CaseRunner.run(root_case(), lambda host: client, "fake cookie")
    assert raised.value is error
    assert events == ["before", "http"]
    effect = Context.get_effect("WRITE")
    assert effect["state"] == "unknown"
    assert effect["snapshot"]["before"] == {"usr_display_bil": "Test User Baseline"}
    assert effect["restore_payload"]["target"] == "json"
    assert len(effect["restore_payload"]["payload"]) == 27
    assert Context.needs_cleanup("WRITE")
    assert not Context.is_case_passed("WRITE")


def test_after_failure_records_unknown_with_restore(monkeypatch):
    events = []
    captures(monkeypatch, events, after_error=ValueError("after failed"))
    with pytest.raises(ValueError, match="after failed"):
        CaseRunner.run(root_case(), lambda host: Client(events), "fake cookie")
    assert events == ["before", "http", "after"]
    assert Context.get_effect_state("WRITE") == "unknown"
    assert Context.get_effect("WRITE")["restore_payload"]["payload"]["usr_display_bil"] == "Test User Baseline"
    assert Context.needs_cleanup("WRITE")


@pytest.mark.parametrize("after_name,state", [("Test User Mutation", "applied"), ("Test User Baseline", "not_applied")])
def test_final_effect_persists_restore_with_diff_state(monkeypatch, after_name, state):
    events = []
    captures(monkeypatch, events, after_name=after_name)
    client = Client(events)
    CaseRunner.run(root_case(), lambda host: client, "fake cookie")
    assert events == ["before", "http", "after"]
    effect = Context.get_effect("WRITE")
    assert effect["state"] == state
    assert effect["restore_payload"]["payload"]["usr_display_bil"] == "Test User Baseline"
    assert len(client.calls[0]["json"]) == 27
    assert Context.is_case_passed("WRITE")


def test_response_assertion_failure_retains_applied_cleanup_material(monkeypatch):
    events = []
    captures(monkeypatch, events)
    case = root_case()
    case["validate"] = [{"type": "json_path_equals", "expression": "$.code", "expected": 200}]
    with pytest.raises(AssertionError):
        CaseRunner.run(case, lambda host: Client(events, response=Response({"code": 500})), "fake cookie")
    assert Context.get_effect_state("WRITE") == "applied"
    assert len(Context.get_effect("WRITE")["restore_payload"]["payload"]) == 27
    assert not Context.is_case_passed("WRITE")


def test_require_change_failure_promotes_not_applied_material_to_unknown(monkeypatch):
    events = []
    captures(monkeypatch, events, after_name="Test User Baseline")
    case = root_case()
    case["effect"]["require_change"] = True
    with pytest.raises(AssertionError, match="diff"):
        CaseRunner.run(case, lambda host: Client(events), "fake cookie")
    assert Context.get_effect_state("WRITE") == "unknown"
    assert Context.get_effect("WRITE")["restore_payload"]["payload"]
    assert Context.needs_cleanup("WRITE")


@pytest.mark.parametrize("region", ["json", "data", "files"])
def test_plain_request_regions_keep_variable_resolution_and_no_effect(region):
    Context.set_variable("value", "resolved")
    events = []
    client = Client(events)
    case = {"id": "PLAIN", "method": "POST", "path": "/offline/{{value}}", region: {"a": "{{value}}"}}
    CaseRunner.run(case, lambda host: client, "fake cookie")
    assert client.calls[0][region] == {"a": "resolved"}
    assert client.calls[0]["path"] == "/offline/resolved"
    assert Context.is_case_passed("PLAIN")
    assert Context.all()["effects"] == {}


def test_non_builder_transport_failure_keeps_legacy_logical_snapshot(monkeypatch):
    events = []
    captures(monkeypatch, events)
    case = root_case()
    case.pop("request_builder")
    case["json"] = {"a": "unchanged legacy body"}
    with pytest.raises(requests.Timeout):
        CaseRunner.run(case, lambda host: Client(events, error=requests.Timeout()), "fake cookie")
    effect = Context.get_effect("WRITE")
    assert effect["snapshot"]["before"] == {"usr_display_bil": "Test User Baseline"}
    assert "restore_payload" not in effect


class FakeRequest:
    class config:
        @staticmethod
        def getoption(name):
            # Offline permission stub only; no destructive CLI invocation.
            return name == "--allow-destructive"


@pytest.mark.parametrize("failure,expected_state", [
    (None, "restored"), ("mutation_timeout", "restored"),
    ("verify", "restored"), ("rollback", "restore_failed"),
    ("verify_rollback", "restore_failed"),
])
def test_real_lifecycle_and_case_runner_preserve_restore_material(monkeypatch, failure, expected_state):
    root = root_case()
    root["tags"] = ["destructive", "update"]
    root["lifecycle"] = {"verify": "VERIFY", "rollback": "ROLLBACK", "verify_rollback": "VERIFY_ROLLBACK"}
    cases = {"WRITE": root}
    for name, depends in (("VERIFY", "WRITE"), ("ROLLBACK", "WRITE"), ("VERIFY_ROLLBACK", "ROLLBACK")):
        cases[name] = {"id": name, "method": "POST", "path": "/offline-" + name.lower(), "depends_on": [depends]}
    cases["ROLLBACK"].update({"tags": ["destructive", "rollback"], "destructive": True,
                              "restore_payload": {"source_effect": "WRITE"}})
    captures(monkeypatch, [], after_name="Test User Mutation")
    calls = []

    class LifecycleClient:
        def request(self, **kwargs):
            path = kwargs["path"]
            calls.append(deepcopy(kwargs))
            if path == "/offline-update":
                assert not Context.has_effect("WRITE")
                if failure == "mutation_timeout":
                    raise requests.Timeout("offline timeout")
            if path == "/offline-rollback":
                effect = Context.get_effect("WRITE")
                assert effect["state"] == ("unknown" if failure == "mutation_timeout" else "applied")
                assert kwargs["json"] == effect["restore_payload"]["payload"]
                assert kwargs["json"]["usr_display_bil"] == "Test User Baseline"
            if path == "/offline-" + str(failure):
                raise AssertionError("offline phase failure")
            return Response()

    run = lambda: LifecycleRunner.run_lifecycle(root, cases, lambda host: LifecycleClient(), "fake cookie", FakeRequest())
    if failure in ("rollback", "verify_rollback"):
        with pytest.raises(LifecycleCleanupError):
            run()
    elif failure == "mutation_timeout":
        with pytest.raises(requests.Timeout):
            run()
    elif failure == "verify":
        with pytest.raises(AssertionError, match="phase failure"):
            run()
    else:
        run()
    paths = [call["path"] for call in calls]
    assert "/offline-rollback" in paths
    assert ("/offline-verify_rollback" in paths) is (failure != "rollback")
    assert Context.get_effect_state("WRITE") == expected_state
    assert len(Context.get_effect("WRITE")["restore_payload"]["payload"]) == 27
