from pathlib import Path

import pytest

from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.case_validator import CaseValidator
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner


class FakeConfig:
    def __init__(self, **options):
        self.options = options

    def getoption(self, name):
        return self.options.get(name, False)


class FakeRequest:
    def __init__(self, **options):
        self.config = FakeConfig(**options)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_contract_experiment_requires_explicit_flag(monkeypatch):
    monkeypatch.setenv("TEST_USERNAME", "ee-Rey-15")
    case = {"contract_experiment": True}
    request = FakeRequest(**{"--tag": "update_contract"})

    with pytest.raises(pytest.skip.Exception):
        LifecycleRunner._check_contract_experiment_permission(case, request)


def test_contract_experiment_requires_explicit_test_username(monkeypatch):
    monkeypatch.delenv("TEST_USERNAME", raising=False)
    case = {"contract_experiment": True}
    request = FakeRequest(
        **{
            "--tag": "update_contract",
            "--allow-contract-experiment": True,
        }
    )

    with pytest.raises(pytest.skip.Exception):
        LifecycleRunner._check_contract_experiment_permission(case, request)


def test_contract_experiment_gate_accepts_all_explicit_conditions(monkeypatch):
    monkeypatch.setenv("TEST_USERNAME", "ee-Rey-15")
    case = {"contract_experiment": True}
    request = FakeRequest(
        **{
            "--tag": "update_contract",
            "--allow-contract-experiment": True,
        }
    )

    LifecycleRunner._check_contract_experiment_permission(case, request)


def test_effect_diff_contract_accepts_only_target_change(monkeypatch):
    monkeypatch.setattr("allure.attach", lambda *args, **kwargs: None)
    case = {
        "id": "EXP",
        "effect": {
            "diff_contract": {
                "required": ["usr_display_bil"],
                "allowed": ["usr_display_bil"],
            }
        },
    }
    snapshot = {
        "diff": {
            "usr_display_bil": {"from": "A", "to": "B"},
        }
    }

    CaseRunner._assert_effect_diff_contract(case, snapshot)


def test_effect_diff_contract_rejects_unexpected_non_target_change(monkeypatch):
    monkeypatch.setattr("allure.attach", lambda *args, **kwargs: None)
    case = {
        "id": "EXP",
        "effect": {
            "diff_contract": {
                "required": ["usr_display_bil"],
                "allowed": ["usr_display_bil"],
            }
        },
    }
    snapshot = {
        "diff": {
            "usr_display_bil": {"from": "A", "to": "B"},
            "roleCode": {"from": "ROLE_A", "to": None},
        }
    }

    with pytest.raises(AssertionError, match="unexpected"):
        CaseRunner._assert_effect_diff_contract(case, snapshot)


def test_effect_diff_contract_rejects_missing_target_change(monkeypatch):
    monkeypatch.setattr("allure.attach", lambda *args, **kwargs: None)
    case = {
        "id": "EXP",
        "effect": {
            "diff_contract": {
                "required": ["usr_display_bil"],
                "allowed": ["usr_display_bil"],
            }
        },
    }

    with pytest.raises(AssertionError, match="missing"):
        CaseRunner._assert_effect_diff_contract(case, {"diff": {}})


def test_update_contract_validator_rejects_extra_payload_field():
    case = {
        "id": "EXP",
        "contract_experiment": True,
        "lifecycle": {"verify": "V", "rollback": "R", "verify_rollback": "VR"},
        "update_contract": {
            "kind": "partial_update",
            "required_request_fields": ["usr_ent_id", "usr_display_bil"],
            "allowed_request_fields": ["usr_ent_id", "usr_display_bil"],
        },
        "json": {
            "usr_ent_id": "1",
            "usr_display_bil": "B",
            "roleCode": "SHOULD_NOT_BE_HERE",
        },
        "effect": {"diff_contract": {"required": ["usr_display_bil"], "allowed": ["usr_display_bil"]}},
    }

    errors = CaseValidator._validate_update_contract_experiment([case])
    assert any("未授权字段" in item and "roleCode" in item for item in errors)


def test_v246_experiment_yaml_static_contract_is_valid():
    root = Path(__file__).resolve().parents[2]
    case_dir = root / "data" / "cases"
    experiment_dir = root / "data" / "experiments"
    endpoint_dir = root / "data" / "endpoints"

    cases = CaseLoader.load_cases(case_dir, endpoint_dir)
    cases += CaseLoader.load_cases(experiment_dir, endpoint_dir)
    CaseValidator.validate_all(cases)

    root_case = next(
        item for item in cases
        if item.get("id") == "WB_USER_UPDATE_CONTRACT_PARTIAL_001"
    )
    assert set(root_case["json"]) == {"usr_ent_id", "usr_display_bil"}
    assert root_case["effect"]["diff_contract"]["allowed"] == ["usr_display_bil"]


def test_api_client_allure_evidence_redacts_cookie_and_password(monkeypatch):
    from wizbank_api_test.core.client import ApiClient

    attachments = []

    class Response:
        status_code = 200
        text = '{"code":200,"password":"secret"}'

        def json(self):
            return {"code": 200, "password": "secret"}

    class Session:
        def request(self, **kwargs):
            assert kwargs["headers"]["Cookie"] == "SESSION=real-secret"
            return Response()

    monkeypatch.setattr(
        "allure.attach",
        lambda body, **kwargs: attachments.append(str(body)),
    )
    client = ApiClient("https://example.invalid")
    client.session = Session()
    client.request(
        method="POST",
        path="/test",
        headers={"Cookie": "SESSION=real-secret"},
        json={"password": "secret", "value": "visible"},
    )

    joined = "\n".join(attachments)
    assert "SESSION=real-secret" not in joined
    assert "'password': 'secret'" not in joined
    assert '"password": "secret"' not in joined
    assert "<REDACTED>" in joined
