"""Offline proof that lifecycle/C1 cleanup stays in the bound execution scope."""

from contextvars import Context as ContextVarsContext
from types import SimpleNamespace

import pytest
import requests

from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.lifecycle_runner import LifecycleCleanupError, LifecycleRunner
from wizbank_api_test.core.runtime_context import (
    RuntimeContext,
    bind_runtime_context,
    get_runtime_context,
)
from wizbank_api_test.services.user_update_acceptance import prepare_dependencies


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    def reject_transport(*args, **kwargs):
        pytest.fail("B3 lifecycle scope tests must not make real HTTP requests")

    monkeypatch.setattr(requests.Session, "request", reject_transport)


def lifecycle_cases():
    return {
        "WRITE": {
            "id": "WRITE", "order": 10, "tags": ["destructive"],
            "lifecycle": {
                "verify": "VERIFY", "rollback": "ROLLBACK", "verify_rollback": "RESTORED",
            },
        },
        "VERIFY": {"id": "VERIFY", "order": 20, "depends_on": "WRITE"},
        "ROLLBACK": {"id": "ROLLBACK", "order": 30, "depends_on": "WRITE"},
        "RESTORED": {"id": "RESTORED", "order": 40, "depends_on": "ROLLBACK"},
    }


@pytest.mark.parametrize("failure", [None, "verify", "rollback"], ids=["success", "verify-failure", "rollback-failure"])
def test_lifecycle_phases_and_finally_cleanup_share_scope_until_unbind(monkeypatch, failure):
    cases = lifecycle_cases()
    runtime_context = RuntimeContext()
    events = []
    provider = object()
    verification_error = AssertionError("offline verify failure")
    rollback_error = RuntimeError("offline rollback failure")

    def capture_before(**kwargs):
        events.append(("before", get_runtime_context()))
        assert Context.get_variable("baseline") == "original"
        return {"field": "original"}

    def capture_after(**kwargs):
        events.append(("after", get_runtime_context()))
        return {"field": Context.get_variable("current")}

    def run(case, api_client_factory, credential_provider, **kwargs):
        assert credential_provider is provider
        case_id = case["id"]
        events.append((case_id, get_runtime_context()))
        if case_id == "WRITE":
            Context.set_variable("baseline", "original")
            before = EffectExecutor.capture_before(case=case)
            Context.set_variable("current", "modified")
            after = EffectExecutor.capture_after(case=case)
            Context.mark_effect("WRITE", snapshot={"before": before, "after": after}, state=Context.EFFECT_APPLIED)
        elif case_id == "VERIFY":
            assert Context.get_variable("current") == "modified"
            assert Context.is_case_passed("WRITE")
            if failure == "verify":
                raise verification_error
        elif case_id == "ROLLBACK":
            assert Context.needs_cleanup("WRITE")
            assert Context.get_effect("WRITE")["snapshot"]["before"] == {"field": "original"}
            assert Context.get_variable("current") == "modified"
            if failure == "rollback":
                raise rollback_error
            Context.set_variable("current", "original")
            EffectExecutor.capture_after(case=case)
        else:
            assert Context.get_variable("current") == "original"
            assert Context.is_case_passed("ROLLBACK")
        Context.mark_case_passed(case_id)

    monkeypatch.setattr(CaseRunner, "run", run)
    monkeypatch.setattr(EffectExecutor, "capture_before", capture_before)
    monkeypatch.setattr(EffectExecutor, "capture_after", capture_after)
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda option: option == "--allow-destructive"))

    def isolated_invocation():
        caught = None
        try:
            with bind_runtime_context(runtime_context):
                assert get_runtime_context() is runtime_context
                LifecycleRunner.run_lifecycle(cases["WRITE"], cases, lambda *args: object(), provider, request)
        except (AssertionError, LifecycleCleanupError) as error:
            caught = error
        with pytest.raises(RuntimeError, match="No active RuntimeContext"):
            get_runtime_context()
        with pytest.raises(RuntimeError, match="No active RuntimeContext"):
            Context.all()
        return caught

    error = ContextVarsContext().run(isolated_invocation)
    phase_ids = [name for name, _ in events if name in cases]
    assert phase_ids == (["WRITE", "VERIFY", "ROLLBACK"] if failure == "rollback"
                         else ["WRITE", "VERIFY", "ROLLBACK", "RESTORED"])
    assert all(active is runtime_context for _, active in events)
    with bind_runtime_context(runtime_context):
        if failure == "rollback":
            assert isinstance(error, LifecycleCleanupError)
            assert error.cleanup_error is rollback_error
            assert Context.get_effect_state("WRITE") == Context.EFFECT_RESTORE_FAILED
        else:
            assert error is (verification_error if failure == "verify" else None)
            assert Context.get_effect_state("WRITE") == Context.EFFECT_RESTORED
            assert Context.get_cleanup_targets("ROLLBACK") == ["WRITE"]


def test_c1_prepare_dependencies_clears_current_scope_only_and_keeps_chain_active(monkeypatch):
    outer = get_runtime_context()
    Context.set_variable("outer", {"values": ["preserved"]})
    Context.mark_case_passed("OUTER_CASE")
    Context.mark_effect("OUTER_EFFECT", state=Context.EFFECT_UNKNOWN)
    original_outer = Context.all()
    inner = RuntimeContext()
    username = "offline-c1-user"
    monkeypatch.setenv("TEST_USERNAME", username)
    dependencies = {
        "FIRST": {"id": "FIRST", "order": 10, "tags": ["readonly"]},
        "SECOND": {"id": "SECOND", "order": 20, "tags": ["readonly"], "depends_on": "FIRST"},
    }
    root = {"id": "WRITE", "order": 30, "depends_on": "SECOND"}
    events = []
    provider = object()

    def run(case, api_client_factory, credential_provider, **kwargs):
        assert credential_provider is provider
        events.append((case["id"], get_runtime_context()))
        assert not Context.has_variable("stale")
        assert not Context.is_case_passed("STALE_CASE")
        assert not Context.has_effect("STALE_EFFECT")
        assert not Context.has_variable("outer")
        if case["id"] == "FIRST":
            assert Context.all() == {"variables": {}, "cases": {}, "effects": {}}
            Context.set_variable("dependency_value", "available")
        else:
            assert Context.is_case_passed("FIRST")
            assert Context.get_variable("dependency_value") == "available"
        Context.mark_case_passed(case["id"])

    monkeypatch.setattr(CaseRunner, "run", run)
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda option: None))
    with bind_runtime_context(inner):
        Context.set_variable("stale", True)
        Context.mark_case_passed("STALE_CASE")
        Context.mark_effect("STALE_EFFECT", state=Context.EFFECT_APPLIED)
        prepare_dependencies(root, dependencies, lambda *args: object(), provider, request, username)
        assert get_runtime_context() is inner
        assert Context.get_variable("dependency_value") == "available"
        assert Context.is_case_passed("SECOND")
    assert events == [("FIRST", inner), ("SECOND", inner)]
    assert get_runtime_context() is outer
    assert Context.all() == original_outer
