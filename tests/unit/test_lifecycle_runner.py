import pytest

from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner


class _Config:
    @staticmethod
    def getoption(name):
        if name == "--allow-destructive":
            return True
        return None


class _Request:
    config = _Config()


def _case_map():
    root = {
        "id": "WRITE_001",
        "name": "write",
        "host": "Host-be",
        "tags": ["destructive", "update"],
        "lifecycle": {
            "verify": "VERIFY_001",
            "rollback": "ROLLBACK_001",
            "verify_rollback": "VERIFY_ROLLBACK_001",
        },
    }
    verify = {
        "id": "VERIFY_001",
        "name": "verify",
        "depends_on": ["WRITE_001"],
        "tags": ["destructive", "verify"],
    }
    rollback = {
        "id": "ROLLBACK_001",
        "name": "rollback",
        "depends_on": ["WRITE_001"],
        "tags": ["destructive", "rollback"],
        "destructive": True,
    }
    verify_rollback = {
        "id": "VERIFY_ROLLBACK_001",
        "name": "verify rollback",
        "depends_on": ["ROLLBACK_001"],
        "tags": ["destructive", "rollback", "verify"],
    }
    return {
        item["id"]: item
        for item in (root, verify, rollback, verify_rollback)
    }


@pytest.fixture(autouse=True)
def reset_context():
    Context.clear()
    yield
    Context.clear()


def test_lifecycle_success_runs_four_phases_and_marks_restored(monkeypatch):
    cases = _case_map()
    calls = []

    def fake_run(case, api_client_factory, admin_cookie, **kwargs):
        case_id = case["id"]
        calls.append(case_id)

        if case_id == "WRITE_001":
            Context.mark_effect(
                "WRITE_001",
                state=Context.EFFECT_APPLIED,
                snapshot={
                    "before": {"field": "A"},
                    "after": {"field": "B"},
                    "diff": {"field": {"from": "A", "to": "B"}},
                },
            )

        Context.mark_case_passed(case_id)

    monkeypatch.setattr(CaseRunner, "run", fake_run)

    LifecycleRunner.run_lifecycle(
        root_case=cases["WRITE_001"],
        case_map=cases,
        api_client_factory=lambda host: object(),
        admin_cookie="SESSION=secret",
        request=_Request(),
    )

    assert calls == [
        "WRITE_001",
        "VERIFY_001",
        "ROLLBACK_001",
        "VERIFY_ROLLBACK_001",
    ]
    assert Context.get_effect_state("WRITE_001") == Context.EFFECT_RESTORED
    assert Context.get_cleanup_targets("ROLLBACK_001") == ["WRITE_001"]


def test_verify_failure_still_runs_cleanup_in_finally(monkeypatch):
    cases = _case_map()
    calls = []

    def fake_run(case, api_client_factory, admin_cookie, **kwargs):
        case_id = case["id"]
        calls.append(case_id)

        if case_id == "WRITE_001":
            Context.mark_effect(
                "WRITE_001",
                state=Context.EFFECT_APPLIED,
                snapshot={
                    "before": {"field": "A"},
                    "after": {"field": "B"},
                    "diff": {"field": {"from": "A", "to": "B"}},
                },
            )
            Context.mark_case_passed(case_id)
            return

        if case_id == "VERIFY_001":
            raise AssertionError("verify failed")

        Context.mark_case_passed(case_id)

    monkeypatch.setattr(CaseRunner, "run", fake_run)

    with pytest.raises(AssertionError, match="verify failed"):
        LifecycleRunner.run_lifecycle(
            root_case=cases["WRITE_001"],
            case_map=cases,
            api_client_factory=lambda host: object(),
            admin_cookie="SESSION=secret",
            request=_Request(),
        )

    assert calls == [
        "WRITE_001",
        "VERIFY_001",
        "ROLLBACK_001",
        "VERIFY_ROLLBACK_001",
    ]
    assert Context.get_effect_state("WRITE_001") == Context.EFFECT_RESTORED


def test_lifecycle_child_map_is_explicit():
    cases = list(_case_map().values())

    mapping = LifecycleRunner.lifecycle_child_map(cases)

    assert mapping == {
        "VERIFY_001": "WRITE_001",
        "ROLLBACK_001": "WRITE_001",
        "VERIFY_ROLLBACK_001": "WRITE_001",
    }
