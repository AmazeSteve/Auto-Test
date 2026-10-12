"""Offline B2 resolver and static DSL contracts."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from wizbank_api_test.core.context import Context
from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.case_validator import CaseValidator
from wizbank_api_test.services.builder_registry import BuilderRegistry
from wizbank_api_test.services.request_payload_resolver import RequestPayloadResolver


ROOT = Path(__file__).resolve().parents[2]


def read_model():
    return json.loads((ROOT / "tests/fixtures/user_update_read_model.json").read_text())


def builder_config():
    return {
        "name": "user_update",
        "source": {"type": "effect_before", "expression": "$.data"},
        "arguments": {"cur_lan": "zh-cn"},
        "mutable_fields": ["usr_display_bil"],
        "overrides": {"usr_display_bil": "Test User Mutation"},
    }


def change(data, path, value):
    for key in path[:-1]:
        data = data[key]
    if value is DELETE:
        data.pop(path[-1], None)
    else:
        data[path[-1]] = value


DELETE = object()
INVALID_BUILDERS = [
    ((), None), ((), []),
    (("name",), "unknown"), (("name",), []),
    (("extra",), True),
    (("source",), DELETE), (("source",), None), (("source",), []),
    (("source", "type"), "snapshot"),
    (("source", "expression"), DELETE), (("source", "expression"), ""),
    (("source", "expression"), 2), (("source", "expression"), "$["),
    (("arguments",), DELETE), (("arguments",), None), (("arguments",), []),
    (("arguments", "cur_lan"), DELETE), (("arguments", "cur_lan"), "  "),
    (("arguments", "cur_lan"), 3), (("arguments", "extra"), "unsupported"),
    (("mutable_fields",), DELETE), (("mutable_fields",), None),
    (("mutable_fields",), []), (("mutable_fields",), "usr_display_bil"),
    (("mutable_fields",), ["usr_display_bil", "usr_display_bil"]),
    (("mutable_fields",), [2]), (("mutable_fields",), [""]),
    (("mutable_fields",), ["gradeId"]),
    (("overrides",), DELETE), (("overrides",), None), (("overrides",), []),
    (("overrides",), {}), (("overrides", "gradeId"), "extra"),
]


@pytest.fixture(autouse=True)
def reset_context():
    Context.clear()
    yield
    Context.clear()


def test_resolver_projects_extracted_model_and_returns_independent_material():
    model = read_model()
    before = {"code": 200, "data": model}
    case = {"request_builder": builder_config()}
    original_before, original_case = deepcopy(before), deepcopy(case)
    result = RequestPayloadResolver.resolve(case, before)
    assert result.target == "json"
    assert result.builder_name == "user_update"
    assert result.changed_fields == {"usr_display_bil"}
    baseline = result.restore_payload["payload"]
    assert result.restore_payload["target"] == "json"
    assert len(baseline) == len(result.body) == 27
    assert baseline["usr_display_bil"] == model["usr_display_bil"]
    assert result.body["usr_display_bil"] == "Test User Mutation"
    assert {key for key in baseline if baseline[key] != result.body[key]} == {"usr_display_bil"}
    assert "userTagVOList" not in baseline
    assert baseline["tagId"] is None
    assert baseline["curLan"] == "zh-cn"
    assert before == original_before and case == original_case
    assert Context.all() == {"variables": {}, "cases": {}, "effects": {}}
    result.body["usr_display_bil"] = "external mutation"
    baseline["usr_ent_id"] = 99
    assert before == original_before and case == original_case


def test_resolver_uses_configured_jsonpath_not_hardcoded_data():
    config = builder_config()
    config["source"]["expression"] = "$.nested.user"
    result = RequestPayloadResolver.resolve(
        {"request_builder": config}, {"nested": {"user": read_model()}}
    )
    assert result.restore_payload["payload"]["usr_ent_id"] == 1830


def test_resolver_obtains_builder_through_registry(monkeypatch):
    from dataclasses import replace
    registration = BuilderRegistry.get("user_update")
    calls = []

    def build(model, **kwargs):
        calls.append(deepcopy(model))
        return registration.build(model, **kwargs)

    monkeypatch.setitem(BuilderRegistry._builders, "user_update", replace(registration, build=build))
    RequestPayloadResolver.resolve({"request_builder": builder_config()}, {"data": read_model()})
    assert calls == [read_model()]
    assert "data" not in calls[0]


@pytest.mark.parametrize("path,value", INVALID_BUILDERS)
def test_invalid_builder_rejected_at_runtime_and_statically(path, value):
    config = builder_config()
    if path:
        change(config, path, value)
    else:
        config = value
    with pytest.raises(ValueError):
        RequestPayloadResolver.resolve({"request_builder": config}, {"data": read_model()})
    assert CaseValidator._validate_request_builder_contract([{
        "id": "WRITE", "request_builder": config,
        "effect": {"capture": {"before": {"endpoint": "read"}, "after": {"endpoint": "read"}}},
    }])


@pytest.mark.parametrize("expression,before", [
    ("$.absent", {"data": {}}),
    ("$.data[*]", {"data": [read_model(), read_model()]}),
])
def test_source_requires_exactly_one_match(expression, before):
    config = builder_config()
    config["source"]["expression"] = expression
    with pytest.raises(ValueError, match="exactly one"):
        RequestPayloadResolver.resolve({"request_builder": config}, before)


def test_data_override_resolution_reuses_variable_resolver_cache():
    config = builder_config()
    config["overrides"]["usr_display_bil"] = "{{$data.unique_display_name}}"
    result = RequestPayloadResolver.resolve({"request_builder": config}, {"data": read_model()})
    assert result.body["usr_display_bil"] == Context.get_variable("data.unique_display_name")
    assert result.body["usr_display_bil"].startswith("api-test-disp-")
    # VariableResolver owns its existing data cache; the Resolver never records Effects.
    assert Context.all()["effects"] == {}


@pytest.mark.parametrize("payload", [{}, None, {"usr_ent_id": 0}])
def test_frozen_builder_validation_is_propagated(payload):
    with pytest.raises(ValueError):
        RequestPayloadResolver.resolve({"request_builder": builder_config()}, {"data": payload})


@pytest.fixture
def cases():
    return CaseLoader.load_cases(ROOT / "data/cases", ROOT / "data/endpoints")


def select(cases, suffix):
    return next(case for case in cases if case["id"] == "WB_USER_DESTRUCTIVE_" + suffix + "_001")


def test_migrated_graph_validates_without_context_and_has_business_assertions(cases):
    assert Context.all()["effects"] == {}
    CaseValidator.validate_all(cases)
    root, rollback = select(cases, "DEMO"), select(cases, "ROLLBACK")
    assert root["request_builder"]["source"]["expression"] == "$.data"
    assert rollback["restore_payload"]["source_effect"] == root["id"]
    for suffix in ("DEMO", "VERIFY", "ROLLBACK", "VERIFY_ROLLBACK"):
        assert {"type": "json_path_equals", "expression": "$.code", "expected": 200} in select(cases, suffix)["validate"]
    for case in (root, rollback):
        assert not {"json", "data", "files"} & set(case)
        assert "restore_mapping" not in case


@pytest.mark.parametrize("suffix,region,value", [
    (suffix, region, value)
    for suffix in ("DEMO", "ROLLBACK")
    for region in ("json", "data", "files")
    for value in (None, {}, {"field": "value"})
] + [("DEMO", "restore_payload", {"source_effect": "other"}),
     ("ROLLBACK", "request_builder", builder_config())])
def test_runtime_body_sources_are_exclusive_even_with_empty_regions(cases, suffix, region, value):
    select(cases, suffix)[region] = value
    with pytest.raises(ValueError, match="不允许同时配置"):
        CaseValidator.validate_all(cases)


@pytest.mark.parametrize("suffix", ["DEMO", "ROLLBACK"])
def test_get_rejects_runtime_body(cases, suffix):
    select(cases, suffix)["method"] = "GET"
    with pytest.raises(ValueError, match="GET用例"):
        CaseValidator.validate_all(cases)


@pytest.mark.parametrize("path,value", [
    (("effect",), DELETE),
    (("effect", "capture"), DELETE),
    (("effect", "capture", "enabled"), False),
    (("effect", "capture", "before"), DELETE),
    (("effect", "capture", "after"), DELETE),
])
def test_builder_requires_enabled_before_and_after(cases, path, value):
    change(select(cases, "DEMO"), path, value)
    with pytest.raises(ValueError, match="requires enabled Effect"):
        CaseValidator.validate_all(cases)


@pytest.mark.parametrize("path,value", [
    (("restore_payload",), None), (("restore_payload",), []),
    (("restore_payload", "extra"), True),
    (("restore_payload", "source_effect"), DELETE),
    (("restore_payload", "source_effect"), "  "),
    (("restore_payload", "source_effect"), "MISSING"),
    (("restore_payload", "source_effect"), 2),
    (("restore_payload", "source_effect"), "WB_USER_DESTRUCTIVE_VERIFY_001"),
    (("depends_on",), ["WB_USER_004"]),
    (("tags",), ["destructive"]), (("destructive",), False),
    (("restore_mapping",), {}),
])
def test_restore_payload_rejects_invalid_graph(cases, path, value):
    change(select(cases, "ROLLBACK"), path, value)
    with pytest.raises(ValueError, match="restore_payload"):
        CaseValidator.validate_all(cases)


@pytest.mark.parametrize("field", ["effect", "request_builder"])
def test_restore_source_requires_effect_and_builder(cases, field):
    select(cases, "DEMO").pop(field)
    assert CaseValidator._validate_restore_payload_contract(cases)


def test_lifecycle_requires_exactly_one_restore_mechanism(cases):
    rollback = select(cases, "ROLLBACK")
    rollback["restore_mapping"] = {"source_effect": select(cases, "DEMO")["id"]}
    assert any("XOR" in error for error in CaseValidator._validate_lifecycle_contract(cases))
    rollback.pop("restore_mapping")
    rollback.pop("restore_payload")
    assert any("XOR" in error for error in CaseValidator._validate_lifecycle_contract(cases))


@pytest.mark.parametrize("suffix", ["DEMO", "ROLLBACK"])
def test_runtime_json_producer_rejects_non_json_content_type(cases, suffix):
    select(cases, suffix)["headers"]["Content-Type"] = "application/x-www-form-urlencoded"
    with pytest.raises(ValueError, match="Content-Type"):
        CaseValidator.validate_all(cases)
