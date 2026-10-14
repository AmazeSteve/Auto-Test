"""C1 entry-point gates and the actual frozen lifecycle, with fake transport only."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import allure
import pytest
import requests

from wizbank_api_test.core.client import ApiClient
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.services import user_update_acceptance as harness
from wizbank_api_test.services.request_payload_resolver import RequestPayloadResolver
from wizbank_api_test.utils.recovery_journal import RecoveryJournal


USERNAME = "test-user-001"


def request_stub(destructive=True, c1=True):
    options = {"--allow-destructive": destructive, "--allow-c1-real-lifecycle": c1}
    return SimpleNamespace(config=SimpleNamespace(getoption=lambda name: options.get(name)))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    old_registry = EndpointRegistry._instance
    EndpointRegistry._instance = None
    Context.clear()
    monkeypatch.setenv("TEST_USERNAME", USERNAME)
    monkeypatch.setenv("C1_CONFIRM_USERNAME", USERNAME)
    monkeypatch.setenv("TEST_PREFIX", "AUTO_TEST_C")

    def reject_network(*args, **kwargs):
        raise AssertionError("C1 unit tests must never contact a real host")

    monkeypatch.setattr(requests.Session, "request", reject_network)
    yield
    Context.clear()
    EndpointRegistry._instance = old_registry


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, cases, journal, mutation_drift=None, final_drift=None, verify_failure=False):
        fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/user_update_read_model.json"
        self.model = json.loads(fixture.read_text())
        self.model.update({"usr_nickname": "{{literal_user_text}}", "usr_extra_2": "$timestamp_ms",
                           "usr_email": "$data.literal_real_value"})
        self.initial = deepcopy(self.model)
        self.cases, self.journal = cases, journal
        self.mutation_drift, self.final_drift = mutation_drift, final_drift
        self.verify_failure = verify_failure
        self.updates, self.calls, self.responses = [], [], []
        self.form_reads = 0

    def request(self, **kwargs):
        path = urlsplit(kwargs["url"]).path
        self.calls.append(deepcopy(kwargs))
        if path == self.cases["WB_USER_GROUP_001"]["path"]:
            payload = {"code": 200, "data": {"userGroupList": [{"usg_code": "ee-Rey用户组", "usg_ent_id": "OFFLINE_GROUP"}]}}
        elif path == VariableResolver.resolve(self.cases["WB_USER_GROUP_004"]["path"]):
            payload = {"code": 200, "data": {"list": [{"usr_ste_usr_id": USERNAME,
                       "usr_ent_id_encrypt": "OFFLINE_TARGET", "usr_eip_id": 1}]}}
        elif path == self.cases["WB_USER_004"]["path"]:
            self.form_reads += 1
            if self.verify_failure and len(self.updates) == 1 and self.form_reads == 4:
                payload = {"code": 500, "data": deepcopy(self.model)}
            else:
                payload = {"code": 200, "data": deepcopy(self.model)}
        elif path == self.cases[harness.ROOT_CASE_ID]["path"]:
            assert self.journal.path is not None and self.journal.path.exists()
            record = json.loads(self.journal.path.read_text())
            assert record["restore_payload"]["payload"]["usr_display_bil"] == self.initial["usr_display_bil"]
            assert len(record["restore_payload"]["payload"]) == 27
            self.updates.append(deepcopy(kwargs["json"]))
            self.model.update(deepcopy(kwargs["json"]))
            tag_text = self.model["tagId"]
            self.model["userTagVOList"] = [{"encryId": token} for token in tag_text.split(",")] if tag_text else []
            leader_text = self.model["directLeaderId"]
            self.model["leaderList"] = [{"usr_ent_id_encrypt": token} for token in leader_text.split(",")] if leader_text else None
            drift = self.mutation_drift if len(self.updates) == 1 else self.final_drift
            if drift:
                field, value = drift
                self.model[field] = deepcopy(value)
            payload = {"code": 200}
        else:
            raise AssertionError("unexpected offline endpoint")
        self.responses.append((payload, deepcopy(payload)))
        return FakeResponse(payload)


@pytest.fixture
def setup_harness(tmp_path, monkeypatch):
    cases = harness.load_cases()
    root = cases[harness.ROOT_CASE_ID]
    journal = RecoveryJournal(tmp_path / ".runtime/recovery")

    def make(**kwargs):
        transport = FakeTransport(cases, journal, **kwargs)
        client = ApiClient("https://example.invalid", verify_ssl=True)
        monkeypatch.setattr(client.session, "request", transport.request)
        return root, cases, journal, transport, lambda host, principal="none": client

    return make


@pytest.mark.parametrize("value", [None, "", "   ", " user "])
def test_explicit_target_is_required_without_default_fallback(value):
    with pytest.raises(harness.C1SafetyError, match="TEST_USERNAME"):
        harness.require_username({} if value is None else {"TEST_USERNAME": value})


@pytest.mark.parametrize("destructive,c1,environ", [
    (False, True, {"TEST_USERNAME": USERNAME, "C1_CONFIRM_USERNAME": USERNAME}),
    (True, False, {"TEST_USERNAME": USERNAME, "C1_CONFIRM_USERNAME": USERNAME}),
    (True, True, {"C1_CONFIRM_USERNAME": USERNAME}),
    (True, True, {"TEST_USERNAME": USERNAME}),
    (True, True, {"TEST_USERNAME": USERNAME, "C1_CONFIRM_USERNAME": "different"}),
])
def test_real_acceptance_requires_all_four_gates(destructive, c1, environ):
    with pytest.raises(harness.C1SafetyError):
        harness.require_real_gates(request_stub(destructive, c1), environ)


@pytest.mark.parametrize("entry", ["preflight", "acceptance"])
def test_entrypoint_skips_before_requesting_login_fixtures(monkeypatch, entry):
    from wizbank_api_test.tests import test_05_user_update_preflight as preflight_entry
    from wizbank_api_test.tests import test_06_user_update_lifecycle_acceptance as acceptance_entry
    monkeypatch.delenv("TEST_USERNAME")
    request = request_stub(destructive=False, c1=False)
    request.getfixturevalue = lambda name: pytest.fail("login fixture must not be requested")
    function = preflight_entry.test_user_update_preflight if entry == "preflight" else acceptance_entry.test_user_update_lifecycle_acceptance
    with pytest.raises(pytest.skip.Exception):
        function(request, monkeypatch)


def test_real_entrypoint_does_not_login_when_confirmation_is_missing(monkeypatch):
    from wizbank_api_test.tests import test_06_user_update_lifecycle_acceptance as entry
    monkeypatch.delenv("C1_CONFIRM_USERNAME")
    request = request_stub()
    request.getfixturevalue = lambda name: pytest.fail("login fixture must not be requested")
    with pytest.raises(pytest.skip.Exception):
        entry.test_user_update_lifecycle_acceptance(request, monkeypatch)


def test_minimum_dependency_chain_reuses_only_existing_readonly_cases(setup_harness):
    root, cases, *_ = setup_harness()
    chain = harness.readonly_dependencies(root, cases)
    assert [case["id"] for case in chain] == ["WB_USER_GROUP_001", "WB_USER_GROUP_004", "WB_USER_004"]
    assert all("readonly" in case["tags"] and not case.get("destructive") for case in chain)


def test_all_dependency_safety_is_checked_before_first_request(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()
    cases["WB_USER_004"]["destructive"] = True
    with pytest.raises(harness.C1AcceptanceError, match="preflight"):
        harness.preflight(root, cases, factory, "offline cookie", request_stub())
    assert transport.calls == [] and journal.path is None


def test_preflight_stops_after_reads_and_returns_hashes_only(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()
    evidence = []
    monkeypatch.setattr(allure, "attach", lambda body, **kw: evidence.append((kw["name"], body)))
    with harness.safe_evidence(monkeypatch):
        summary = harness.preflight(root, cases, factory, "private-session-cookie", request_stub())
    assert summary["READY"] == "YES" and summary["field_count"] == 27
    assert summary["changed_fields"] == ["usr_display_bil"]
    assert transport.updates == [] and len(transport.calls) == 4
    assert Context.get_variable("data.unique_display_name").startswith("AUTO_TEST_C-disp-")
    assert journal.path is None
    assert [name for name, _ in evidence] == ["C1 preflight summary"]
    text = json.dumps(evidence)
    for raw in [USERNAME, "private-session-cookie", "Test User Baseline", "$data.literal_real_value"]:
        assert raw not in text
    assert "baseline_sha256" in text and "mutation_sha256" in text and "restore_sha256" in text


@pytest.mark.parametrize("kind", ["preflight", "acceptance"])
def test_returned_identity_mismatch_fails_before_builder_or_mutation(setup_harness, monkeypatch, kind):
    root, cases, journal, transport, factory = setup_harness()
    transport.model["usr_ste_usr_id"] = "different-returned-user"
    calls = []
    monkeypatch.setattr(RequestPayloadResolver, "resolve", lambda *args: calls.append(args))
    with harness.safe_evidence(monkeypatch):
        with pytest.raises((harness.C1SafetyError, harness.C1AcceptanceError)):
            if kind == "preflight":
                harness.preflight(root, cases, factory, "offline cookie", request_stub())
            else:
                harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch, journal)
    assert calls == [] and transport.updates == [] and journal.path is None


def test_happy_path_uses_real_lifecycle_writes_journal_before_transport_and_deletes_after_semantics(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()
    run = LifecycleRunner.run_lifecycle
    lifecycle_calls, resolved_objects, captured_payloads, evidence = [], [], [], []
    resolve = RequestPayloadResolver.resolve

    def observed_lifecycle(*args, **kwargs):
        lifecycle_calls.append(args[0]["id"])
        return run(*args, **kwargs)

    def observed_resolver(*args, **kwargs):
        assert journal.path is None
        result = resolve(*args, **kwargs)
        resolved_objects.append((result, deepcopy(result)))
        return result

    def observe_capture(original):
        def capture(*args, **kwargs):
            payload = original(*args, **kwargs)
            captured_payloads.append((payload, deepcopy(payload)))
            return payload
        return capture

    monkeypatch.setattr(LifecycleRunner, "run_lifecycle", observed_lifecycle)
    monkeypatch.setattr(RequestPayloadResolver, "resolve", observed_resolver)
    monkeypatch.setattr(EffectExecutor, "capture_before", observe_capture(EffectExecutor.capture_before))
    monkeypatch.setattr(EffectExecutor, "capture_after", observe_capture(EffectExecutor.capture_after))
    monkeypatch.setattr(allure, "attach", lambda body, **kw: evidence.append((kw["name"], body)))
    with harness.safe_evidence(monkeypatch):
        summary = harness.accept_lifecycle(root, cases, factory, "private-session-cookie", request_stub(), monkeypatch, journal)
    assert lifecycle_calls == [harness.ROOT_CASE_ID]
    assert len(transport.updates) == 2 and transport.form_reads == 6
    assert journal.status == "deleted" and not journal.path.exists()
    assert summary["effect_state"] == "restored" and summary["rollback_executed"]
    assert summary["mutation_changed_fields"] == ["usr_display_bil"]
    assert summary["restored_changed_fields"] == []
    assert Context.get_effect(root["id"])["restore_payload"] == resolved_objects[0][0].restore_payload
    assert all(original == copied for original, copied in captured_payloads)
    assert all(original == copied for original, copied in resolved_objects)
    assert all(original == copied for original, copied in transport.responses)
    assert transport.updates[-1] == resolved_objects[0][0].restore_payload["payload"]
    assert transport.model["usr_display_bil"] == transport.initial["usr_display_bil"]
    text = json.dumps(evidence)
    for raw in [USERNAME, "private-session-cookie", "Test User Baseline", "$data.literal_real_value", "{{literal_user_text}}"]:
        assert raw not in text
    assert "C1 mutation semantic diff" in text and "C1 restored semantic diff" in text
    assert "sha256" in text and "usr_display_bil" in text


@pytest.mark.parametrize("field,value", [
    ("userGroupId", "different-group"), ("gradeId", "different-grade"),
    ("roleCode", "NLRN_1"), ("userTagVOList", [{"encryId": "different-tag"}]),
    ("leaderList", [{"usr_ent_id_encrypt": "different-leader"}]),
])
def test_non_target_mutation_drift_fails_acceptance_and_retains_journal(setup_harness, monkeypatch, field, value):
    root, cases, journal, transport, factory = setup_harness(mutation_drift=(field, value))
    with harness.safe_evidence(monkeypatch):
        with pytest.raises(harness.C1AcceptanceError, match="recovery journal"):
            harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch, journal)
    assert journal.path.exists() and journal.status == "retained"
    assert len(transport.updates) == 2


def test_final_business_drift_retains_journal_even_when_frozen_lifecycle_says_restored(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness(final_drift=("usr_nickname", "unexpected-final-value"))
    with harness.safe_evidence(monkeypatch):
        with pytest.raises(harness.C1AcceptanceError):
            harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch, journal)
    assert Context.get_effect_state(root["id"]) == "restored"
    assert journal.path.exists() and journal.status == "retained"


def test_lifecycle_exception_retains_journal_after_existing_finally_cleanup(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness(verify_failure=True)
    with harness.safe_evidence(monkeypatch):
        with pytest.raises(harness.C1AcceptanceError):
            harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch, journal)
    assert len(transport.updates) == 2
    assert journal.path.exists() and journal.status == "retained"


def test_journal_storage_failure_stops_before_mutation(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()

    def fail_write(*args):
        raise OSError("offline simulated unwritable recovery directory")

    monkeypatch.setattr(journal, "create", fail_write)
    with harness.safe_evidence(monkeypatch):
        with pytest.raises(harness.C1AcceptanceError):
            harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch, journal)
    assert transport.updates == [] and journal.path is None


def test_final_evidence_error_does_not_delete_recovery_material(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()
    attached = []

    def evidence(body, **kwargs):
        if kwargs["name"] == "C1 lifecycle summary" and json.loads(body)["READY"] == "YES":
            raise OSError("offline simulated evidence storage failure")
        attached.append((kwargs["name"], body))

    monkeypatch.setattr(allure, "attach", evidence)
    with harness.safe_evidence(monkeypatch):
        with pytest.raises(harness.C1AcceptanceError):
            harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch, journal)
    assert journal.path.exists() and journal.status == "retained"
    assert Context.get_effect_state(root["id"]) == "restored"
    assert json.loads(attached[-1][1])["READY"] == "NO"


def test_lazy_login_attachments_are_suppressed_by_entrypoint_scope(monkeypatch):
    from wizbank_api_test.tests import test_05_user_update_preflight as entry
    evidence = []
    monkeypatch.setattr(allure, "attach", lambda body, **kw: evidence.append((kw["name"], body)))
    monkeypatch.setattr(entry, "load_cases", lambda: {harness.ROOT_CASE_ID: {"id": harness.ROOT_CASE_ID}})

    def fixture(name):
        allure.attach("private-login-cookie-and-body", name="request")
        return object()

    def preflight(*args):
        harness._attach("C1 preflight summary", {"READY": "YES", "target_user_hash": "safe-hash"})

    request = request_stub()
    request.getfixturevalue = fixture
    monkeypatch.setattr(entry, "preflight", preflight)
    entry.test_user_update_preflight(request, monkeypatch)
    assert evidence == [("C1 preflight summary", '{"READY": "YES", "target_user_hash": "safe-hash"}')]


def test_default_pending_journal_blocks_acceptance_without_reads_or_new_record(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()
    journal.directory.mkdir(parents=True)
    old = journal.directory / "old.json"
    contents = b"private-pending-recovery-material\xff"
    old.write_bytes(contents)
    # Redirect the default constructor to the offline directory, not the live checkout.
    monkeypatch.setattr(RecoveryJournal.__init__, "__defaults__", (journal.directory,))
    evidence = []
    monkeypatch.setattr(allure, "attach", lambda body, **kw: evidence.append(body))
    with harness.safe_evidence(monkeypatch):
        with pytest.raises(harness.C1AcceptanceError, match="count=1") as caught:
            harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch)
    assert transport.updates == [] and transport.calls == []
    assert journal.path is None and list(journal.directory.iterdir()) == [old]
    assert old.read_bytes() == contents
    assert "private-pending-recovery-material" not in str(caught.value)
    summary = json.loads(evidence[-1])
    assert summary["pending_recovery"] == {"count": 1, "directory": str(journal.directory)}
    assert "private-pending-recovery-material" not in "".join(evidence)


def test_pending_created_during_preparation_is_blocked_by_create_guard(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()
    prepare = harness.prepare_dependencies
    contents = b"private-newly-discovered-recovery-material"
    old = journal.directory / "late-pending.json"

    def preparation(*args, **kwargs):
        prepare(*args, **kwargs)
        journal.directory.mkdir(parents=True)
        old.write_bytes(contents)

    monkeypatch.setattr(harness, "prepare_dependencies", preparation)
    with harness.safe_evidence(monkeypatch):
        with pytest.raises(harness.C1AcceptanceError, match="count=1"):
            harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch, journal)
    assert transport.updates == [] and journal.path is None
    assert old.read_bytes() == contents and list(journal.directory.iterdir()) == [old]


def test_preflight_leaves_pending_journal_untouched(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()
    journal.directory.mkdir(parents=True)
    old = journal.directory / "old.json"
    contents = b"private-pending-recovery-material"
    old.write_bytes(contents)
    with harness.safe_evidence(monkeypatch):
        summary = harness.preflight(root, cases, factory, "offline cookie", request_stub())
    assert summary["READY"] == "YES" and transport.updates == []
    assert old.read_bytes() == contents and list(journal.directory.iterdir()) == [old]


def test_stale_tmp_file_does_not_block_happy_path_and_is_not_deleted(setup_harness, monkeypatch):
    root, cases, journal, transport, factory = setup_harness()
    journal.directory.mkdir(parents=True)
    temporary = journal.directory / ".journal-abandoned.tmp"
    temporary.write_bytes(b"private-incomplete-record")
    with harness.safe_evidence(monkeypatch):
        summary = harness.accept_lifecycle(root, cases, factory, "offline cookie", request_stub(), monkeypatch, journal)
    assert summary["effect_state"] == "restored" and journal.status == "deleted"
    assert len(transport.updates) == 2 and not journal.path.exists()
    assert temporary.read_bytes() == b"private-incomplete-record"
