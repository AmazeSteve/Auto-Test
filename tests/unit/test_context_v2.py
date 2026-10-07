import pytest

from wizbank_api_test.core.context import Context
from wizbank_api_test.core.extractor import Extractor
from wizbank_api_test.core.variable_resolver import VariableResolver


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = str(payload)
        self.headers = {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def reset_context():
    Context.clear()
    yield
    Context.clear()


def test_legacy_set_writes_into_variables_namespace():
    Context.set("target_usr_ent_id", "ENC-123")

    data = Context.all()

    assert data["variables"]["target_usr_ent_id"] == "ENC-123"
    assert "target_usr_ent_id" not in data
    assert Context.get("target_usr_ent_id") == "ENC-123"


def test_extractor_writes_only_into_variables_namespace():
    response = FakeResponse(
        {
            "data": {
                "usr_ent_id": 1830,
            }
        }
    )

    extracted = Extractor.run_extractors(
        response,
        {
            "current_user_ent_id": {
                "type": "json_path",
                "expression": "$.data.usr_ent_id",
            }
        },
    )

    assert extracted == {"current_user_ent_id": 1830}
    assert Context.get_variable("current_user_ent_id") == 1830
    assert "current_user_ent_id" not in Context.all()


def test_variable_resolver_reads_context_v2_variable():
    Context.set_variable("original_usr_display_bil", "Rey-15-full")

    resolved = VariableResolver.resolve(
        {
            "name": "{{original_usr_display_bil}}",
            "message": "before={{original_usr_display_bil}}",
        }
    )

    assert resolved["name"] == "Rey-15-full"
    assert resolved["message"] == "before=Rey-15-full"


def test_clear_resets_variables_cases_and_effects():
    Context.set_variable("target_usr_ent_id", "ENC-123")
    Context.mark_case_passed("CASE_001")
    Context.mark_effect(
        "WRITE_001",
        state=Context.EFFECT_APPLIED,
        snapshot={"diff": {"field": {"from": "A", "to": "B"}}},
    )

    Context.clear()

    assert Context.all() == {
        "variables": {},
        "cases": {},
        "effects": {},
    }


def test_snapshot_size_counts_namespace_entries_not_root_keys():
    Context.set_variable("a", 1)
    Context.set_variable("b", 2)
    Context.mark_case_passed("CASE_001")
    Context.mark_effect("WRITE_001", state=Context.EFFECT_APPLIED)

    snapshot = Context.snapshot()

    assert snapshot["size"] == 4
    assert set(snapshot["data"]) == {"variables", "cases", "effects"}
