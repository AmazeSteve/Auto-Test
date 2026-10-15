"""Offline isolation and compatibility contracts for scoped runtime state."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context as PythonContext
from threading import Barrier

import pytest

from wizbank_api_test.core.context import Context
from wizbank_api_test.core.runtime_context import (
    RuntimeContext,
    bind_runtime_context,
    get_runtime_context,
)


def _populate(label):
    Context.set_variable("value", {"owner": label})
    Context.mark_case_passed(f"case-{label}")
    Context.mark_cleanup_targets(f"case-{label}", [f"effect-{label}"])
    Context.mark_effect(f"effect-{label}", metadata={"owner": label})


def test_instances_have_exact_independent_namespaces():
    first, second = RuntimeContext(), RuntimeContext()
    assert first.data == second.data == {"variables": {}, "cases": {}, "effects": {}}
    assert first.data is not second.data
    for namespace in first.data:
        assert first.data[namespace] is not second.data[namespace]
    assert vars(first) == {"data": first.data}
    assert "_data" not in Context.__dict__


def test_variables_cases_and_effects_are_isolated_between_bindings():
    first, second = RuntimeContext(), RuntimeContext()
    with bind_runtime_context(first):
        _populate("first")
        first_snapshot = Context.all()
    with bind_runtime_context(second):
        assert Context.snapshot() == {
            "size": 0,
            "data": {"variables": {}, "cases": {}, "effects": {}},
        }
        _populate("second")
        assert not Context.is_case_passed("case-first")
        assert not Context.has_effect("effect-first")
        assert Context.get_cleanup_targets("case-first") == []
    with bind_runtime_context(first):
        assert Context.all() == first_snapshot
        assert not Context.is_case_passed("case-second")
        assert not Context.has_effect("effect-second")


def test_clear_resets_current_only_and_preserves_parent_store():
    parent, child = RuntimeContext(), RuntimeContext()
    with bind_runtime_context(parent):
        _populate("parent")
        parent_snapshot = Context.all()
        with bind_runtime_context(child):
            _populate("child")
            Context.clear()
            assert get_runtime_context() is child
            assert Context.all() == {"variables": {}, "cases": {}, "effects": {}}
        assert get_runtime_context() is parent
        assert Context.all() == parent_snapshot


@pytest.mark.parametrize(
    "method,args",
    [
        ("set_variable", ("key", "value")),
        ("get_variable", ("key",)),
        ("has_variable", ("key",)),
        ("clear_variables", ()),
        ("mark_case_passed", ("case",)),
        ("is_case_passed", ("case",)),
        ("mark_cleanup_targets", ("case", ["effect"])),
        ("get_cleanup_targets", ("case",)),
        ("mark_effect", ("effect",)),
        ("get_effect", ("effect",)),
        ("has_effect", ("effect",)),
        ("get_effect_state", ("effect",)),
        ("needs_cleanup", ("effect",)),
        ("mark_effect_restored", ("effect",)),
        ("mark_effect_restore_failed", ("effect",)),
        ("promote_not_applied_to_unknown", ("effect", ValueError("failed"))),
        ("clear", ()),
        ("all", ()),
        ("snapshot", ()),
        ("set", ("key", "value")),
        ("get", ("key",)),
    ],
)
def test_each_facade_api_fails_closed_without_active_context(method, args):
    with pytest.raises(RuntimeError, match="^No active RuntimeContext$"):
        PythonContext().run(getattr(Context, method), *args)


def test_get_runtime_context_fails_closed_without_binding():
    with pytest.raises(RuntimeError, match="^No active RuntimeContext$"):
        PythonContext().run(get_runtime_context)


def test_nested_binding_restores_exact_parent_after_normal_exit():
    parent, child = RuntimeContext(), RuntimeContext()
    with bind_runtime_context(parent):
        Context.set("owner", "parent")
        with bind_runtime_context(child) as bound:
            assert bound is child
            assert get_runtime_context() is child
            Context.set("owner", "child")
        assert get_runtime_context() is parent
        assert Context.get("owner") == "parent"


def test_exception_binding_restores_exact_parent():
    parent, child = RuntimeContext(), RuntimeContext()
    error = ValueError("scope failure")
    with bind_runtime_context(parent):
        _populate("parent")
        with pytest.raises(ValueError) as caught:
            with bind_runtime_context(child):
                _populate("child")
                raise error
        assert caught.value is error
        assert get_runtime_context() is parent
        assert Context.get_variable("value") == {"owner": "parent"}


def test_binding_restores_absent_context_after_exception():
    def execute():
        with pytest.raises(RuntimeError, match="scope failure"):
            with bind_runtime_context(RuntimeContext()):
                Context.set("temporary", True)
                raise RuntimeError("scope failure")
        with pytest.raises(RuntimeError, match="^No active RuntimeContext$"):
            Context.all()

    PythonContext().run(execute)


def test_snapshot_and_all_copy_only_current_state():
    parent, child = RuntimeContext(), RuntimeContext()
    with bind_runtime_context(parent):
        _populate("parent")
        with bind_runtime_context(child):
            _populate("child")
            snapshot, all_data = Context.snapshot(), Context.all()
            assert snapshot["size"] == 3
            assert snapshot["data"] == all_data == child.data
            assert "parent" not in repr(snapshot)
            snapshot["data"]["variables"]["value"]["owner"] = "changed"
            all_data["effects"]["effect-child"]["metadata"]["owner"] = "changed"
            assert Context.get("value") == {"owner": "child"}
            assert Context.get_effect("effect-child")["metadata"] == {"owner": "child"}
        assert Context.get("value") == {"owner": "parent"}


def test_legacy_set_get_delegate_to_current_variables_only():
    with bind_runtime_context(RuntimeContext()):
        Context.set("legacy", "value")
        assert Context.get("legacy") == Context.get_variable("legacy") == "value"
        assert Context.get("missing", "fallback") == "fallback"
        assert Context.all() == {
            "variables": {"legacy": "value"}, "cases": {}, "effects": {},
        }


@pytest.mark.parametrize(
    "state,applied,cleanup",
    [
        ("unknown", False, True),
        ("not_applied", False, False),
        ("applied", True, True),
        ("restored", False, False),
        ("restore_failed", False, True),
    ],
)
def test_existing_five_mark_effect_states_remain_exact(state, applied, cleanup):
    runtime = RuntimeContext()
    with bind_runtime_context(runtime):
        Context.mark_effect("effect", state=state)
        assert Context.get_effect_state("effect") == state
        assert Context.get_effect("effect")["applied"] is applied
        assert Context.needs_cleanup("effect") is cleanup


def test_restore_transitions_keep_existing_evidence_and_cleanup_behavior():
    first, second = RuntimeContext(), RuntimeContext()
    with bind_runtime_context(first):
        payload = {"target": "json", "payload": {"user": {"name": "baseline"}}}
        Context.mark_effect("effect", restore_payload=payload)
        payload["payload"]["user"]["name"] = "mutated input"
        Context.mark_effect_restore_failed("effect", "rollback", ValueError("failure"))
        assert Context.get_effect("effect")["applied"] is True
        assert Context.needs_cleanup("effect") is True
        assert Context.get_effect("effect")["restore_error"] == "failure"
        Context.mark_effect_restored("effect", "successful-rollback")
        assert Context.get_effect("effect")["applied"] is False
        assert Context.needs_cleanup("effect") is False
        assert Context.get_effect("effect")["restore_payload"]["payload"]["user"] == {
            "name": "baseline",
        }
    with bind_runtime_context(second):
        assert Context.get_effect("effect") is None


def test_not_applied_promotion_remains_current_and_preserves_restore_copy():
    parent, child = RuntimeContext(), RuntimeContext()
    payload = {"target": "json", "payload": {"name": "baseline"}}
    with bind_runtime_context(parent):
        Context.mark_effect("effect", state="not_applied", restore_payload=payload)
        with bind_runtime_context(child):
            Context.mark_effect("effect", state="not_applied", restore_payload=payload)
            assert Context.promote_not_applied_to_unknown("effect", AssertionError())
            assert Context.get_effect_state("effect") == "unknown"
            assert Context.get_effect("effect")["post_send_failure"] == {
                "reason": "post_send_processing_failed", "error_type": "AssertionError",
            }
            assert Context.get_effect("effect")["restore_payload"] == payload
            assert Context.get_effect("effect")["restore_payload"] is not payload
        assert Context.get_effect_state("effect") == "not_applied"


def test_clear_variables_preserves_current_cases_and_effects():
    with bind_runtime_context(RuntimeContext()):
        _populate("current")
        Context.clear_variables()
        assert Context.all()["variables"] == {}
        assert Context.is_case_passed("case-current")
        assert Context.has_effect("effect-current")


def test_structure_repair_is_local_to_current_instance():
    first, second = RuntimeContext(), RuntimeContext()
    first.data = {"variables": [], "cases": None}
    with bind_runtime_context(first):
        assert Context.all() == {"variables": {}, "cases": {}, "effects": {}}
    assert second.data == {"variables": {}, "cases": {}, "effects": {}}
    first.data = None
    with bind_runtime_context(first):
        Context.mark_case_passed("repaired")
        assert Context.is_case_passed("repaired")
    assert second.data["cases"] == {}


def test_invalid_binding_does_not_replace_current_context():
    parent = RuntimeContext()
    with bind_runtime_context(parent):
        with pytest.raises(TypeError, match="must be a RuntimeContext"):
            with bind_runtime_context(None):
                pytest.fail("Invalid binding entered")
        assert get_runtime_context() is parent


def test_thread_context_bindings_are_independent():
    barrier = Barrier(2)

    def worker(label):
        with pytest.raises(RuntimeError, match="No active RuntimeContext"):
            get_runtime_context()
        runtime = RuntimeContext()
        with bind_runtime_context(runtime):
            _populate(label)
            barrier.wait(timeout=10)
            assert get_runtime_context() is runtime
            assert Context.get("value") == {"owner": label}
            return Context.all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, label) for label in ("first", "second")]
        first, second = [future.result(timeout=15) for future in futures]
    assert first["cases"] == {"case-first": {"passed": True, "cleanup_targets": ["effect-first"]}}
    assert second["cases"] == {"case-second": {"passed": True, "cleanup_targets": ["effect-second"]}}


def test_async_task_bindings_restore_parent_and_remain_independent():
    parent = RuntimeContext()

    async def execute():
        async def worker(label):
            runtime = RuntimeContext()
            with bind_runtime_context(runtime):
                _populate(label)
                await asyncio.sleep(0)
                assert get_runtime_context() is runtime
                assert Context.get("value") == {"owner": label}
                result = Context.all()
            assert get_runtime_context() is parent
            return result

        return await asyncio.gather(worker("first"), worker("second"))

    with bind_runtime_context(parent):
        Context.set("value", "parent")
        first, second = asyncio.run(execute())
        assert get_runtime_context() is parent
        assert Context.get("value") == "parent"
        assert first["variables"]["value"] == {"owner": "first"}
        assert second["variables"]["value"] == {"owner": "second"}
