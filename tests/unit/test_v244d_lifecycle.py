from pathlib import Path

import pytest

from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.endpoint_registry import EndpointRegistry


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


class StatefulUserClient:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})

        if path == "/app/v2/admin/user/get_user_excel_form":
            return FakeResponse(
                {
                    "code": 200,
                    "message": "success",
                    "data": {
                        "usr_ent_id": 1830,
                        "usr_ste_usr_id": "ee-Rey-15",
                        "usr_display_bil": self.state["usr_display_bil"],
                        "userGroupId": self.state["userGroupId"],
                        "usgDisplayBil": self.state["usgDisplayBil"],
                        "gradeId": self.state["gradeId"],
                        "ugrDisplayBil": self.state["ugrDisplayBil"],
                    },
                }
            )

        if path == "/app/v2/admin/user/upd_user":
            body = kwargs.get("json") or {}
            self.state["usr_display_bil"] = body["usr_display_bil"]
            self.state["userGroupId"] = body["userGroupId"]
            self.state["gradeId"] = body["gradeId"]
            return FakeResponse({"code": 200})

        raise AssertionError(f"unexpected path: {path}")


class _Config:
    @staticmethod
    def getoption(name):
        if name == "--allow-destructive":
            return True
        return None


class _Request:
    config = _Config()


@pytest.fixture()
def destructive_cases(monkeypatch):
    old_instance = EndpointRegistry._instance
    EndpointRegistry._instance = None
    Context.clear()
    monkeypatch.setenv("TEST_PREFIX", "wb")
    monkeypatch.setenv("TEST_USERNAME", "ee-Rey-15")

    cases = CaseLoader.load_cases(CASE_DIR, ENDPOINT_DIR)
    case_map = {case["id"]: case for case in cases}

    Context.set("target_usr_ent_id", "ENC-123")
    Context.set("current_user_ent_id", 1830)
    Context.set("original_usr_display_bil", "Original Name")
    Context.set("original_usr_ste_usr_id", "ee-Rey-15")
    Context.set("original_user_group_id", "GROUP-1")
    Context.set("original_usg_display_bil", "Rey用户组")
    Context.set("original_grade_id", "GRADE-1")
    Context.set("original_ugr_display_bil", "Unspecified")
    Context.mark_case_passed("WB_USER_004")

    try:
        yield case_map
    finally:
        EndpointRegistry._instance = old_instance
        Context.clear()


def test_dynamic_mutation_and_explicit_restore_mapping(destructive_cases):
    state = {
        "usr_display_bil": "Original Name",
        "userGroupId": "GROUP-1",
        "usgDisplayBil": "Rey用户组",
        "gradeId": "GRADE-1",
        "ugrDisplayBil": "Unspecified",
    }
    client = StatefulUserClient(state)

    def factory(host_key):
        assert host_key == "Host-be"
        return client

    request = _Request()
    cookie = "SESSION=secret"

    LifecycleRunner.run_lifecycle(
        root_case=destructive_cases["WB_USER_DESTRUCTIVE_DEMO_001"],
        case_map=destructive_cases,
        api_client_factory=factory,
        admin_cookie=cookie,
        request=request,
    )

    generated = Context.get_variable("data.unique_display_name")
    assert generated.startswith("wb-disp-")
    assert generated != "Original Name"

    # 生命周期结束后必须回到原状态。
    assert state["usr_display_bil"] == "Original Name"
    assert state["userGroupId"] == "GROUP-1"
    assert state["gradeId"] == "GRADE-1"

    rollback_calls = [
        call
        for call in client.calls
        if call["path"] == "/app/v2/admin/user/upd_user"
    ]
    assert len(rollback_calls) == 2
    assert rollback_calls[0]["json"]["usr_display_bil"] == generated
    assert rollback_calls[-1]["json"]["usr_display_bil"] == "Original Name"
    assert rollback_calls[-1]["json"]["userGroupId"] == "GROUP-1"
    assert rollback_calls[-1]["json"]["gradeId"] == "GRADE-1"

    assert Context.get_effect_state(
        "WB_USER_DESTRUCTIVE_DEMO_001"
    ) == Context.EFFECT_RESTORED
