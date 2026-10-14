"""Offline cleanup eligibility after a successful full-update transport return."""

from copy import deepcopy
from types import SimpleNamespace

import allure
import pytest
import requests

from wizbank_api_test.core.assertions import Assertor
from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.extractor import Extractor
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.services.request_payload_resolver import RequestPayloadResolver
from wizbank_api_test.tests.unit.test_b2_runtime import (
    Client,
    FakeRequest,
    Response,
    captures,
    root_case,
)


PRIVATE_EXCEPTION = "Cookie=private; Authorization=private; password=private; token=private"
POST_SEND_FAILURES = (
    "require_change",
    "response_assertion",
    "validation_resolution",
    "extract_resolution",
    "extractor",
    "extracted_evidence",
    "effect_evidence",
    "mark_case_passed",
    "context_completion",
    "effect_record_completion",
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    Context.clear()

    def reject_network(*args, **kwargs):
        raise AssertionError("real HTTP is forbidden in post-send cleanup unit tests")

    monkeypatch.setattr(requests.Session, "request", reject_network)
    yield
    Context.clear()


def _case():
    case = root_case()
    # Preserve more than the changed-field projection, including nested record data.
    for field in ("usr_nickname", "usr_extra_2"):
        case["effect"]["target"]["fields"].append(field)
        case["effect"]["capture"]["fields"][field] = f"$.data.{field}"
    return case


def _observe_effect(monkeypatch):
    original = Context.mark_effect
    saved = []

    def mark(*args, **kwargs):
        original(*args, **kwargs)
        case_id = kwargs.get("case_id", args[0] if args else None)
        record = Context.get_effect(case_id)
        record["extra_record_field"] = {"nested": ["existing", {"count": 2}]}
        saved.append((record, deepcopy(record)))

    monkeypatch.setattr(Context, "mark_effect", mark)
    return saved


def _inject_failure(monkeypatch, case, failure):
    error = AssertionError(PRIVATE_EXCEPTION) if failure == "response_assertion" else RuntimeError(PRIVATE_EXCEPTION)

    def fail(*args, **kwargs):
        raise error

    if failure == "require_change":
        case["effect"]["require_change"] = True
        return AssertionError, None
    if failure == "response_assertion":
        monkeypatch.setattr(Assertor, "run_validations", fail)
    elif failure in ("validation_resolution", "extract_resolution"):
        key = "validate" if failure == "validation_resolution" else "extract"
        if key == "extract":
            case[key] = {"extracted_code": {"type": "json_path", "expression": "$.code"}}
        rules = case[key]
        original = VariableResolver.resolve

        def resolve(value):
            if value is rules:
                raise error
            return original(value)

        monkeypatch.setattr(VariableResolver, "resolve", resolve)
    elif failure == "extractor":
        monkeypatch.setattr(Extractor, "run_extractors", fail)
    elif failure in ("extracted_evidence", "effect_evidence"):
        if failure == "extracted_evidence":
            case["extract"] = {"extracted_code": {"type": "json_path", "expression": "$.code"}}
        expected_name = "extracted variables" if failure == "extracted_evidence" else "effect snapshot - WRITE"
        original = allure.attach

        def attach(*args, **kwargs):
            if kwargs.get("name") == expected_name:
                raise error
            return original(*args, **kwargs)

        monkeypatch.setattr(allure, "attach", attach)
    elif failure == "mark_case_passed":
        monkeypatch.setattr(Context, "mark_case_passed", fail)
    elif failure == "context_completion":
        case["extract"] = {"extracted_code": {"type": "json_path", "expression": "$.code"}}
        original = Context.set_variable

        def set_variable(key, value):
            if key == "extracted_code":
                raise error
            return original(key, value)

        monkeypatch.setattr(Context, "set_variable", set_variable)
    elif failure == "effect_record_completion":
        original = Context.mark_effect

        def mark(*args, **kwargs):
            original(*args, **kwargs)
            raise error

        monkeypatch.setattr(Context, "mark_effect", mark)
    else:
        raise AssertionError(f"unknown offline failure scenario: {failure}")
    return type(error), error


def _assert_preserved(record, original, expected_state):
    assert record["state"] == expected_state
    assert record["applied"] is (expected_state == Context.EFFECT_APPLIED)
    assert record["snapshot"] == original["snapshot"]
    assert record["metadata"] == original["metadata"]
    assert record["restore_payload"] == original["restore_payload"]
    assert len(record["restore_payload"]["payload"]) == 27
    assert record["extra_record_field"] == original["extra_record_field"]
    # Apart from state and safe diagnostics, no pre-existing record field is lost.
    for key, value in original.items():
        if key not in ("state", "applied"):
            assert record[key] == value


@pytest.mark.parametrize("failure", POST_SEND_FAILURES)
def test_not_applied_post_send_failure_promotes_existing_record_without_losing_evidence(monkeypatch, failure):
    events = []
    case = _case()
    captures(monkeypatch, events, after_name="Test User Baseline")
    saved = _observe_effect(monkeypatch)
    expected_error, error = _inject_failure(monkeypatch, case, failure)
    client = Client(events)

    with pytest.raises(expected_error) as raised:
        CaseRunner.run(case, lambda host: client, "offline cookie")

    if error is not None:
        assert raised.value is error
    assert events == ["before", "http", "after"]
    assert len(client.calls) == 1
    assert len(client.calls[0]["json"]) == 27
    assert len(saved) == 1, "post-send promotion must not replace the Effect record"
    previous_record, previous = saved[0]
    record = Context.get_effect("WRITE")
    assert record is previous_record
    assert previous["state"] == Context.EFFECT_NOT_APPLIED
    assert previous["snapshot"]["diff"] == {}
    assert previous["snapshot"]["before"] == previous["snapshot"]["after"]
    assert set(previous["snapshot"]["before"]) == {"usr_display_bil", "usr_nickname", "usr_extra_2"}
    _assert_preserved(record, previous, Context.EFFECT_UNKNOWN)
    assert Context.needs_cleanup("WRITE")
    assert not Context.is_case_passed("WRITE")
    diagnostics = {key: value for key, value in record.items() if key not in previous}
    assert diagnostics == {
        "post_send_failure": {
            "reason": "post_send_processing_failed",
            "error_type": type(raised.value).__name__,
        }
    }
    assert PRIVATE_EXCEPTION not in repr(diagnostics)


@pytest.mark.parametrize("failure", ("validation_resolution", "extract_resolution", "extractor"))
def test_real_missing_variable_and_jsonpath_errors_promote_not_applied(monkeypatch, failure):
    case = _case()
    if failure == "validation_resolution":
        case["validate"] = [{"type": "status_code", "expected": "{{missing_validation_value}}"}]
    elif failure == "extract_resolution":
        case["extract"] = {"result": {"type": "json_path", "expression": "{{missing_extract_path}}"}}
    else:
        case["extract"] = {"result": {"type": "json_path", "expression": "$.missing_response_value"}}
    events = []
    captures(monkeypatch, events, after_name="Test User Baseline")
    saved = _observe_effect(monkeypatch)
    with pytest.raises(ValueError):
        CaseRunner.run(case, lambda host: Client(events), "offline cookie")
    assert events == ["before", "http", "after"]
    assert len(saved) == 1
    record, previous = saved[0]
    assert Context.get_effect("WRITE") is record
    _assert_preserved(record, previous, Context.EFFECT_UNKNOWN)
    assert record["post_send_failure"] == {"reason": "post_send_processing_failed", "error_type": "ValueError"}
    assert Context.needs_cleanup("WRITE")
    assert not Context.is_case_passed("WRITE")


@pytest.mark.parametrize("failure", ("response_assertion", "effect_evidence", "effect_record_completion"))
def test_applied_post_send_failure_preserves_applied_state_and_full_evidence(monkeypatch, failure):
    events = []
    captures(monkeypatch, events)
    case = _case()
    saved = _observe_effect(monkeypatch)
    expected_error, error = _inject_failure(monkeypatch, case, failure)
    with pytest.raises(expected_error) as raised:
        CaseRunner.run(case, lambda host: Client(events), "offline cookie")
    assert raised.value is error
    assert len(saved) == 1
    record, previous = saved[0]
    assert Context.get_effect("WRITE") is record
    assert previous["snapshot"]["diff"] == {
        "usr_display_bil": {"from": "Test User Baseline", "to": "Test User Mutation"}
    }
    _assert_preserved(record, previous, Context.EFFECT_APPLIED)
    assert "post_send_failure" not in record
    assert Context.needs_cleanup("WRITE")
    assert not Context.is_case_passed("WRITE")


@pytest.mark.parametrize("failure", ("require_change", "response_assertion", "validation_resolution", "extract_resolution", "extractor"))
def test_legacy_not_applied_failure_remains_not_applied(monkeypatch, failure):
    events = []
    captures(monkeypatch, events, after_name="Test User Baseline")
    case = _case()
    case.pop("request_builder")
    case["json"] = {"legacy": "ordinary YAML JSON"}
    expected_error, _ = _inject_failure(monkeypatch, case, failure)
    with pytest.raises(expected_error):
        CaseRunner.run(case, lambda host: Client(events), "offline cookie")
    record = Context.get_effect("WRITE")
    assert events == ["before", "http", "after"]
    assert record["state"] == Context.EFFECT_NOT_APPLIED
    assert "restore_payload" not in record
    assert "post_send_failure" not in record
    assert not Context.needs_cleanup("WRITE")


def test_builder_without_explicit_restore_material_does_not_promote(monkeypatch):
    events = []
    captures(monkeypatch, events, after_name="Test User Baseline")
    original = RequestPayloadResolver.resolve

    def resolve(case, before):
        resolved = original(case, before)
        return SimpleNamespace(target="json", body=resolved.body, restore_payload=None)

    monkeypatch.setattr(RequestPayloadResolver, "resolve", resolve)
    case = root_case()
    expected_error, _ = _inject_failure(monkeypatch, case, "response_assertion")
    with pytest.raises(expected_error):
        CaseRunner.run(case, lambda host: Client(events), "offline cookie")
    record = Context.get_effect("WRITE")
    assert events == ["before", "http", "after"]
    assert record["state"] == Context.EFFECT_NOT_APPLIED
    assert record["restore_payload"] is None
    assert "post_send_failure" not in record
    assert not Context.needs_cleanup("WRITE")


def test_builder_missing_restore_attribute_fails_before_transport_without_creating_effect(monkeypatch):
    events = []
    captures(monkeypatch, events)
    monkeypatch.setattr(RequestPayloadResolver, "resolve", lambda case, before: SimpleNamespace(target="json", body={}))
    client = Client(events)
    with pytest.raises(AttributeError):
        CaseRunner.run(root_case(), lambda host: client, "offline cookie")
    assert events == ["before"]
    assert client.calls == []
    assert not Context.has_effect("WRITE")
    assert not Context.needs_cleanup("WRITE")


@pytest.mark.parametrize("failure", ("response_assertion", "extractor", "mark_case_passed"))
def test_non_effect_failure_does_not_create_effect(monkeypatch, failure):
    case = root_case()
    case.pop("request_builder")
    case.pop("effect")
    case["json"] = {"plain": "body"}
    expected_error, _ = _inject_failure(monkeypatch, case, failure)
    events = []
    with pytest.raises(expected_error):
        CaseRunner.run(case, lambda host: Client(events), "offline cookie")
    assert events == ["http"]
    assert Context.all()["effects"] == {}


@pytest.mark.parametrize("failure", ("context_all", "context_snapshot", "context_attachment", "runtime_snapshot_attachment"))
def test_failures_after_mark_case_passed_do_not_promote_successful_not_applied(monkeypatch, failure):
    events = []
    captures(monkeypatch, events, after_name="Test User Baseline")
    error = RuntimeError(PRIVATE_EXCEPTION)

    def fail(*args, **kwargs):
        raise error

    if failure == "context_all":
        monkeypatch.setattr(Context, "all", fail)
    elif failure == "context_snapshot":
        monkeypatch.setattr(Context, "snapshot", fail)
    else:
        expected_name = "context after effect" if failure == "context_attachment" else "runtime context snapshot"
        original = allure.attach

        def attach(*args, **kwargs):
            if kwargs.get("name") == expected_name:
                raise error
            return original(*args, **kwargs)

        monkeypatch.setattr(allure, "attach", attach)

    with pytest.raises(RuntimeError) as raised:
        CaseRunner.run(root_case(), lambda host: Client(events), "offline cookie")
    assert raised.value is error
    assert events == ["before", "http", "after"]
    assert Context.is_case_passed("WRITE")
    assert Context.get_effect_state("WRITE") == Context.EFFECT_NOT_APPLIED
    assert not Context.needs_cleanup("WRITE")
    assert "post_send_failure" not in Context.get_effect("WRITE")


@pytest.mark.parametrize("failure", ("request_timeout", "request_connection", "after_capture"))
def test_existing_request_and_after_capture_failures_keep_unknown_cleanup(monkeypatch, failure):
    events = []
    error_type = {"request_timeout": requests.Timeout, "request_connection": requests.ConnectionError, "after_capture": ValueError}[failure]
    error = error_type("offline original failure")
    captures(monkeypatch, events, after_error=error if failure == "after_capture" else None)
    client = Client(events, error=None if failure == "after_capture" else error)
    with pytest.raises(error_type) as raised:
        CaseRunner.run(root_case(), lambda host: client, "offline cookie")
    assert raised.value is error
    record = Context.get_effect("WRITE")
    assert record["state"] == Context.EFFECT_UNKNOWN
    assert record["snapshot"]["after"] is None
    assert len(record["restore_payload"]["payload"]) == 27
    assert Context.needs_cleanup("WRITE")


@pytest.mark.parametrize("failure", ("after_capture", "snapshot_build", "effect_write"))
def test_failure_before_current_snapshot_is_recorded_does_not_promote_stale_effect(monkeypatch, failure):
    case = root_case()
    Context.mark_effect(
        "WRITE",
        metadata={"invocation": "previous"},
        snapshot={"before": {"prior": 1}, "after": {"prior": 1}, "diff": {}},
        state=Context.EFFECT_NOT_APPLIED,
        restore_payload={"target": "json", "payload": {"prior": "stale restore"}},
    )
    events = []
    error = RuntimeError("offline pre-record failure")
    captures(monkeypatch, events, after_name="Test User Baseline", after_error=error if failure == "after_capture" else None)
    if failure == "snapshot_build":
        def build_snapshot(*args, **kwargs):
            raise error

        monkeypatch.setattr(CaseRunner, "_build_effect_snapshot", build_snapshot)
    elif failure == "effect_write":
        original = Context.mark_effect
        attempted = False

        def mark(*args, **kwargs):
            nonlocal attempted
            if not attempted:
                attempted = True
                raise error
            return original(*args, **kwargs)

        monkeypatch.setattr(Context, "mark_effect", mark)

    class StaleAwareClient(Client):
        def request(self, **kwargs):
            self.events.append("http")
            self.calls.append(deepcopy(kwargs))
            return Response()

    with pytest.raises(RuntimeError) as raised:
        CaseRunner.run(case, lambda host: StaleAwareClient(events), "offline cookie")
    assert raised.value is error
    assert events == ["before", "http", "after"]
    record = Context.get_effect("WRITE")
    # The existing request/after failure path records this invocation's before,
    # rather than claiming a complete after capture using the previous record.
    assert record["state"] == Context.EFFECT_UNKNOWN
    assert record["snapshot"] == {
        "before": {"usr_display_bil": "Test User Baseline"}, "after": None, "diff": {}
    }
    assert record["metadata"] == case["effect"]
    assert len(record["restore_payload"]["payload"]) == 27
    assert "post_send_failure" not in record
    assert Context.needs_cleanup("WRITE")


def _lifecycle_cases():
    root = root_case()
    root["tags"] = ["destructive", "update"]
    root["lifecycle"] = {"verify": "VERIFY", "rollback": "ROLLBACK", "verify_rollback": "VERIFY_ROLLBACK"}
    cases = {"WRITE": root}
    for name, depends in (("VERIFY", "WRITE"), ("ROLLBACK", "WRITE"), ("VERIFY_ROLLBACK", "ROLLBACK")):
        cases[name] = {"id": name, "method": "POST", "path": "/offline-" + name.lower(), "depends_on": [depends]}
    cases["ROLLBACK"].update({"tags": ["destructive", "rollback"], "destructive": True, "restore_payload": {"source_effect": "WRITE"}})
    return root, cases


def test_real_lifecycle_runs_rollback_and_verify_rollback_after_post_send_failure(monkeypatch):
    root, cases = _lifecycle_cases()
    captures(monkeypatch, [], after_name="Test User Baseline")
    calls = []
    restore_used = []
    primary_error = AssertionError("offline post-send assertion failed")
    original_validations = Assertor.run_validations

    def validations(response, rules):
        if response.payload.get("phase") == "mutation":
            raise primary_error
        return original_validations(response, rules)

    monkeypatch.setattr(Assertor, "run_validations", validations)

    class LifecycleClient:
        def request(self, **kwargs):
            path = kwargs["path"]
            calls.append(deepcopy(kwargs))
            if path == "/offline-update":
                assert not Context.has_effect("WRITE")
                assert len(kwargs["json"]) == 27
                return Response({"code": 200, "phase": "mutation"})
            if path == "/offline-rollback":
                record = Context.get_effect("WRITE")
                assert record["state"] == Context.EFFECT_UNKNOWN
                assert not Context.is_case_passed("WRITE")
                assert record["snapshot"]["diff"] == {}
                assert record["snapshot"]["before"] == record["snapshot"]["after"]
                assert kwargs["json"] == record["restore_payload"]["payload"]
                assert kwargs["json"]["usr_display_bil"] == "Test User Baseline"
                assert len(kwargs["json"]) == 27
                restore_used.append(deepcopy(kwargs["json"]))
            if path == "/offline-verify_rollback":
                assert Context.is_case_passed("ROLLBACK")
            return Response()

    with pytest.raises(AssertionError) as raised:
        LifecycleRunner.run_lifecycle(root, cases, lambda host: LifecycleClient(), "offline cookie", FakeRequest())
    assert raised.value is primary_error
    assert [call["path"] for call in calls] == ["/offline-update", "/offline-rollback", "/offline-verify_rollback"]
    record = Context.get_effect("WRITE")
    assert record["state"] == Context.EFFECT_RESTORED
    assert record["restore_payload"]["payload"] == restore_used[0]
    assert Context.is_case_passed("ROLLBACK")
    assert Context.is_case_passed("VERIFY_ROLLBACK")
    assert not Context.needs_cleanup("WRITE")


def test_successful_not_applied_lifecycle_stays_not_applied_and_skips_rollback(monkeypatch):
    root, cases = _lifecycle_cases()
    captures(monkeypatch, [], after_name="Test User Baseline")
    calls = []

    class LifecycleClient:
        def request(self, **kwargs):
            calls.append(deepcopy(kwargs))
            return Response()

    result = LifecycleRunner.run_lifecycle(root, cases, lambda host: LifecycleClient(), "offline cookie", FakeRequest())
    assert [call["path"] for call in calls] == ["/offline-update", "/offline-verify"]
    assert result["state"] == Context.EFFECT_NOT_APPLIED
    assert result["snapshot"]["diff"] == {}
    assert len(result["restore_payload"]["payload"]) == 27
    assert Context.is_case_passed("WRITE")
    assert Context.is_case_passed("VERIFY")
    assert not Context.is_case_passed("ROLLBACK")
    assert not Context.is_case_passed("VERIFY_ROLLBACK")
    assert not Context.needs_cleanup("WRITE")


def test_effect_state_contract_remains_exactly_five_existing_states():
    states = {name: value for name, value in vars(Context).items() if name.startswith("EFFECT_")}
    assert states == {
        "EFFECT_UNKNOWN": "unknown",
        "EFFECT_NOT_APPLIED": "not_applied",
        "EFFECT_APPLIED": "applied",
        "EFFECT_RESTORED": "restored",
        "EFFECT_RESTORE_FAILED": "restore_failed",
    }
