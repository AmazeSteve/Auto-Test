"""v2.4.4B / v2.4.4C Effect Runtime 单元回归。"""

from pathlib import Path

import pytest

from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_collector import EffectCollector
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENDPOINT_DIR = PROJECT_ROOT / "data" / "endpoints"
CASE_DIR = PROJECT_ROOT / "data" / "cases"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.fixture()
def loaded_cases():
    old_instance = EndpointRegistry._instance
    EndpointRegistry._instance = None

    try:
        cases = CaseLoader.load_cases(
            CASE_DIR,
            ENDPOINT_DIR,
        )
        yield cases
    finally:
        EndpointRegistry._instance = old_instance
        Context.clear()


def test_effect_collector_reads_nested_jsonpath():
    snapshot = EffectCollector.build_snapshot(
        before={
            "code": 200,
            "data": {"usr_display_bil": "A"},
        },
        after={
            "code": 200,
            "data": {"usr_display_bil": "B"},
        },
        fields=["usr_display_bil"],
        selectors={
            "usr_display_bil": "$.data.usr_display_bil"
        },
    )

    assert snapshot["before"] == {
        "usr_display_bil": "A"
    }
    assert snapshot["after"] == {
        "usr_display_bil": "B"
    }
    assert snapshot["diff"] == {
        "usr_display_bil": {
            "from": "A",
            "to": "B",
        }
    }


def test_effect_capture_reuses_registry_auth_and_variables(
    loaded_cases,
):
    case = next(
        item
        for item in loaded_cases
        if item["id"] == "WB_USER_DESTRUCTIVE_DEMO_001"
    )

    Context.set("target_usr_ent_id", "ENC-123")

    client = FakeClient(
        FakeResponse(
            {
                "code": 200,
                "data": {"usr_display_bil": "A"},
            }
        )
    )
    requested_hosts = []

    def factory(host_key):
        requested_hosts.append(host_key)
        return client

    payload = EffectExecutor.capture_before(
        case=case,
        api_client_factory=factory,
        admin_cookie="SESSION=abc",
        header_builder=CaseRunner.build_header,
    )

    assert payload["code"] == 200
    assert requested_hosts == ["Host-be"]

    request_data = client.calls[0]
    assert request_data["method"] == "POST"
    assert (
        request_data["path"]
        == "/app/v2/admin/user/get_user_excel_form"
    )
    assert request_data["headers"]["Cookie"] == "SESSION=abc"
    assert (
        request_data["headers"]["Content-Type"]
        == "application/x-www-form-urlencoded"
    )
    assert request_data["data"]["usr_ent_id"] == "ENC-123"
    assert str(request_data["data"]["pdate"]).isdigit()


def test_effect_capture_rejects_business_failure(loaded_cases):
    case = next(
        item
        for item in loaded_cases
        if item["id"] == "WB_USER_DESTRUCTIVE_DEMO_001"
    )

    Context.set("target_usr_ent_id", "ENC-123")
    client = FakeClient(
        FakeResponse(
            {
                "code": 500,
                "message": "decrypt fail!",
            }
        )
    )

    with pytest.raises(AssertionError, match="decrypt fail"):
        EffectExecutor.capture_before(
            case=case,
            api_client_factory=lambda host_key: client,
            admin_cookie="SESSION=abc",
            header_builder=CaseRunner.build_header,
        )


def test_rollback_can_bypass_failed_case_when_effect_applied():
    Context.clear()
    Context.mark_effect(
        "DESTRUCTIVE_001",
        state=Context.EFFECT_APPLIED,
        snapshot={"diff": {"field": {"from": "A", "to": "B"}}},
    )

    rollback_case = {
        "tags": ["destructive", "rollback"],
        "destructive": True,
        "depends_on": ["DESTRUCTIVE_001"],
    }

    bypass = LifecycleRunner.get_cleanup_dependencies(
        rollback_case
    )

    assert bypass == {"DESTRUCTIVE_001"}
    assert Context.is_case_passed("DESTRUCTIVE_001") is False

    # 不应因 destructive Case 未 mark_case_passed 而 skip。
    depends_on = LifecycleRunner.check_dependencies(
        rollback_case,
        bypass_case_passed=bypass,
    )
    assert depends_on == ["DESTRUCTIVE_001"]
