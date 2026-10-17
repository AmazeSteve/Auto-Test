"""Offline framework evidence, runtime merge compatibility and redaction."""

import json
from pathlib import Path
from shutil import copytree

import pytest
import yaml

from tools.api_inventory.case_adapter import _merge_case_with_endpoint, scan_cases
from tools.api_inventory.registry_adapter import scan_registry


REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests/fixtures/api_inventory/framework"


def _fixture_repo(tmp_path):
    destination = tmp_path / "framework"
    copytree(FIXTURE, destination)
    return destination


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(records, sort_keys=False), encoding="utf-8")


def test_actual_framework_counts_and_registered_api_case_utilization():
    registry = scan_registry(REPO_ROOT)
    cases = scan_cases(REPO_ROOT, registry.records)
    assert registry.counts == {"registry_definitions": 19}
    assert cases.counts == {"enabled_cases": 28, "disabled_cases": 2}
    assert not registry.issues
    assert not cases.issues
    registered = {record["api_uid"] for record in registry.records}
    covered = {record["api_uid"] for record in cases.records if record["enabled"]}
    assert len(registered) == 19
    assert registered <= covered
    assert len(registered & covered) / len(registered) == 1
    update = next(record for record in registry.records if record["endpoint_id"] == "EP_USER_UPDATE")
    assert update["runtime_path"] == "/app/v2/admin/user/upd_user"
    assert update["canonical_path"] == "/v2/admin/user/upd_user"
    assert update["destructive"]


def test_duplicate_registry_is_reported_and_all_definitions_retained():
    registry = scan_registry(FIXTURE)
    assert registry.counts["registry_definitions"] == 4
    assert len(registry.records) == 4
    duplicate = next(issue for issue in registry.issues if issue["code"] == "DUPLICATE_REGISTRY_DEFINITION")
    assert duplicate["endpoint_ids"] == ["ITEM", "ITEM_ALIAS"]
    assert duplicate["count"] == 2


def test_disabled_cases_remain_raw_evidence_and_overrides_follow_effective_api():
    registry = scan_registry(FIXTURE)
    cases = scan_cases(FIXTURE, registry.records)
    assert cases.counts == {"enabled_cases": 3, "disabled_cases": 2}
    by_id = {record["case_id"]: record for record in cases.records}
    item = next(record for record in registry.records if record["endpoint_id"] == "ITEM")
    assert by_id["NORMAL"]["api_uid"] == item["api_uid"]
    assert by_id["NORMAL"]["tags"] == ["case-only", "readonly", "shared"]
    assert not by_id["NORMAL"]["endpoint_override"]
    assert by_id["DISABLED"]["api_uid"] == item["api_uid"]
    assert not by_id["DISABLED"]["enabled"]
    assert not by_id["DISABLED_ALIAS"]["enabled"]
    assert by_id["OVERRIDE"]["method"] == "DELETE"
    assert by_id["OVERRIDE"]["canonical_path"] == "/items/{item_id}/archive"
    assert by_id["OVERRIDE"]["endpoint_override"]
    assert by_id["OVERRIDE"]["api_uid"] != item["api_uid"]
    issues = [issue for issue in cases.issues if issue["code"] == "ENDPOINT_IDENTITY_OVERRIDE"]
    assert {issue["case_id"] for issue in issues} == {"OVERRIDE", "DISPATCH_OVERRIDE"}


@pytest.mark.parametrize("case_patch", [
    {},
    {"method": "DELETE", "path": "/other/{id}", "params": {"new": 1}},
    {"headers": {"Shared": "case", "Added": "value"}, "tags": ["same", "case"]},
    {"name": None, "module": None, "destructive": False, "json": {"replacement": True}},
    {"auth": "none", "host": "Host-other", "data": {}, "tags": []},
])
def test_independent_merge_matches_frozen_normalizer_and_does_not_mutate(case_patch):
    # Runtime imports are confined to this compatibility test, never analyzer.
    from wizbank_api_test.core.case_normalizer import CaseNormalizer

    endpoint = {"id": "ENDPOINT", "_file": "private.yaml", "name": "default", "module": "Inventory",
                "method": "POST", "path": "/app/items", "host": "Host-be", "auth": "admin",
                "default_tags": ["same", "endpoint"], "destructive": True,
                "headers": {"Shared": "endpoint", "Default": "value"},
                "params": {"default": 1}, "json": {"default": 1}, "data": {"default": 1}}
    case = {"id": "CASE", "endpoint": "ENDPOINT", **case_patch}
    original = json.dumps([case, endpoint], sort_keys=True)
    effective = _merge_case_with_endpoint(case, endpoint)
    assert effective == CaseNormalizer.merge_case_with_endpoint(case, endpoint)
    assert json.dumps([case, endpoint], sort_keys=True) == original
    assert "endpoint" not in effective
    assert "_file" not in effective
    assert "default_tags" not in effective
    assert effective["_endpoint_id"] == "ENDPOINT"
    assert "destructive" in effective["tags"]


def test_framework_outputs_never_contain_query_header_auth_host_or_body_values():
    registry = scan_registry(FIXTURE)
    cases = scan_cases(FIXTURE, registry.records)
    serialized = json.dumps({"registry": registry.__dict__, "cases": cases.__dict__}, sort_keys=True)
    assert "CANARY_" not in serialized
    for record in registry.records + cases.records:
        assert not {"headers", "params", "json", "data", "auth", "host", "body"} & record.keys()
        assert "?" not in record["runtime_path"]
    assert registry.records[0]["host_class"] == "ABSOLUTE_URL"
    assert registry.records[0]["auth_required"] is True


def test_legacy_identity_obeys_shallow_form_override_and_excludes_ordinary_params():
    registry = scan_registry(FIXTURE)
    cases = scan_cases(FIXTURE, registry.records)
    endpoint = next(record for record in registry.records if record["endpoint_id"] == "DISPATCH")
    case = next(record for record in cases.records if record["case_id"] == "DISPATCH_OVERRIDE")
    assert endpoint["dispatch_signature"] == ["cmd=get_item", "module=admin"]
    assert case["dispatch_signature"] == ["cmd=list_items", "module=admin"]
    assert case["endpoint_override"]
    assert "pageNum" not in json.dumps(case["identity"])


@pytest.mark.parametrize("flags,enabled", [
    ({}, True), ({"enable": False}, False), ({"enabled": False, "enable": True}, False),
    ({"enabled": True, "enable": False}, True), ({"enabled": 0}, True),
    ({"enabled": "false"}, True), ({"enabled": None}, True),
])
def test_enabled_semantics_match_literal_false_runtime_rule(tmp_path, flags, enabled):
    root = _fixture_repo(tmp_path)
    _write(root / "data/cases/cases.yaml", [{"id": "CASE", "endpoint": "ITEM", **flags}])
    cases = scan_cases(root, scan_registry(root).records)
    assert cases.records[0]["enabled"] is enabled
    assert cases.counts["enabled_cases"] == int(enabled)
    assert cases.counts["disabled_cases"] == int(not enabled)


def test_missing_or_ambiguous_endpoint_never_becomes_case_coverage(tmp_path):
    root = _fixture_repo(tmp_path)
    _write(root / "data/endpoints/duplicate.yaml", [{"id": "ITEM", "method": "GET", "path": "/different"}])
    _write(root / "data/cases/cases.yaml", [
        {"id": "MISSING", "endpoint": "NONEXISTENT", "method": "GET", "path": "/explicit"},
        {"id": "AMBIGUOUS", "endpoint": "ITEM", "method": "GET", "path": "/explicit"},
    ])
    registry = scan_registry(root)
    assert any(issue["code"] == "DUPLICATE_ENDPOINT_ID" for issue in registry.issues)
    cases = scan_cases(root, registry.records)
    assert {record["resolution_status"] for record in cases.records} == {"MISSING_ENDPOINT_REFERENCE", "AMBIGUOUS_ENDPOINT_REFERENCE"}
    assert all("api_uid" not in record for record in cases.records)


def test_missing_legacy_dispatch_after_case_override_stays_unresolved(tmp_path):
    root = _fixture_repo(tmp_path)
    _write(root / "data/cases/cases.yaml", [{"id": "CASE", "endpoint": "DISPATCH", "data": {"cmd": "get_item"}}])
    cases = scan_cases(root, scan_registry(root).records)
    assert cases.records[0]["resolution_status"] == "LEGACY_UNRESOLVED"
    assert "api_uid" not in cases.records[0]


@pytest.mark.parametrize("invalidity", ["disabled_child", "missing_child_endpoint", "missing_capture_endpoint", "shared_child"])
def test_lifecycle_requires_complete_enabled_recovery_contract(tmp_path, invalidity):
    root = _fixture_repo(tmp_path)
    cases = [
        {"id": "ROOT", "endpoint": "WRITE", "effect": {"capture": {"before": {"endpoint": "ITEM"}, "after": {"endpoint": "ITEM"}}},
         "request_builder": {"name": "fixture"}, "lifecycle": {"verify": "VERIFY", "rollback": "ROLLBACK", "verify_rollback": "VERIFY_ROLLBACK"}},
        {"id": "VERIFY", "endpoint": "ITEM", "tags": ["verify"], "depends_on": ["ROOT"]},
        {"id": "ROLLBACK", "endpoint": "WRITE", "tags": ["rollback"], "depends_on": ["ROOT"], "restore_payload": {"source_effect": "ROOT"}},
        {"id": "VERIFY_ROLLBACK", "endpoint": "ITEM", "tags": ["rollback", "verify"], "depends_on": ["ROLLBACK"]},
    ]
    _write(root / "data/cases/cases.yaml", cases)
    registry = scan_registry(root)
    records = scan_cases(root, registry.records).records
    assert next(record for record in records if record["case_id"] == "ROOT")["safe_lifecycle"]
    if invalidity == "disabled_child":
        cases[2]["enabled"] = False
    elif invalidity == "missing_child_endpoint":
        cases[2]["endpoint"] = "MISSING"
    elif invalidity == "missing_capture_endpoint":
        cases[0]["effect"]["capture"]["before"]["endpoint"] = "MISSING"
    else:
        cases.append({**cases[0], "id": "OTHER_ROOT"})
    _write(root / "data/cases/cases.yaml", cases)
    records = scan_cases(root, registry.records).records
    assert not any(record["safe_lifecycle"] for record in records)


@pytest.mark.parametrize("content", ["not: a-list", "- string", "- id: ITEM\n  json: [CANARY_PARSE_SECRET"])
def test_malformed_yaml_fails_with_safe_relative_error(tmp_path, content):
    root = _fixture_repo(tmp_path)
    path = root / "data/endpoints/endpoints.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        scan_registry(root)
    assert "data/endpoints/endpoints.yaml" in str(error.value)
    assert "CANARY" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_framework_scan_is_deterministic_and_has_no_runtime_imports():
    registry_first = scan_registry(FIXTURE)
    registry_second = scan_registry(FIXTURE)
    assert registry_first == registry_second
    assert scan_cases(FIXTURE, registry_first.records) == scan_cases(FIXTURE, registry_second.records)
    for relative in ("tools/api_inventory/registry_adapter.py", "tools/api_inventory/case_adapter.py"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "from wizbank_api_test" not in source
        assert "import core" not in source
