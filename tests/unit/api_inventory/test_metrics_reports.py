"""Offline semantics and disclosure boundaries of inventory conclusions."""

import copy
import csv
import json
from pathlib import Path

import pytest

from tools.api_inventory.classifier import classify
from tools.api_inventory.coverage import calculate_coverage
from tools.api_inventory.identity import api_uid, display_identity, make_identity
from tools.api_inventory.priority import build_priority
from tools.api_inventory.reporter import write_reports


def _api(path="/users", method="GET", *, source=True, postman=True, registry=False,
         enabled=(), disabled=(), api_kind="HTTP_API", **source_flags):
    identity = make_identity("wizbank", method, path)
    record = {
        "api_uid": api_uid(identity), "identity": identity,
        "display_identity": display_identity(identity), "runtime_paths": ["/app" + path],
        "canonical_paths": [path], "source_records": [], "postman_records": [],
        "registry_records": [], "case_records": [], "endpoint_ids": [],
        "enabled_case_ids": list(enabled), "disabled_case_ids": list(disabled),
        "issues": [], "confidence": "HIGH" if source and postman else "MEDIUM",
        "resolution_status": "RESOLVED",
    }
    if source:
        record["source_records"] = [{
            "origin": "source", "file": "Controller.java", "line": 10,
            "controller": "demo.Controller", "java_method": "users", "api_kind": api_kind,
            "active": True, "deprecated": False, "internal": False,
            "resolution_status": "RESOLVED", **source_flags,
        }]
    if postman:
        record["postman_records"] = [{
            "origin": "postman", "file": "collection/request.yaml", "request_name": "Users",
            "method": method, "runtime_path": "/app" + path,
            "header_keys": ["Authorization", "Cookie"], "query_keys": ["page"],
            "body_keys": ["password", "client_secret"], "body_type": "json",
        }]
    if registry:
        record["registry_records"] = [{"origin": "registry", "file": "data/endpoints/users.yaml", "endpoint_id": "users", "destructive": False}]
        record["endpoint_ids"] = ["users"]
    return record


def test_classification_distinguishes_source_proof_from_postman_evidence():
    source_and_postman = _api()
    postman_only = _api("/other", source=False)
    source_only = _api("/source", postman=False)
    classify([source_and_postman, postman_only, source_only])
    assert (source_and_postman["eligibility"], source_and_postman["coverage_status"]) == ("ELIGIBLE", "SOURCE_POSTMAN_UNREGISTERED")
    assert (postman_only["eligibility"], postman_only["coverage_status"]) == ("UNRESOLVED", "POSTMAN_ONLY")
    assert (source_only["eligibility"], source_only["coverage_status"]) == ("ELIGIBLE", "SOURCE_ONLY")


@pytest.mark.parametrize("flags,reason", [
    ({"api_kind": "PAGE_ROUTE"}, "PAGE_ROUTE"),
    ({"deprecated": True}, "DEPRECATED"),
    ({"internal": True}, "INTERNAL"),
    ({"active": False}, "INACTIVE"),
])
def test_reliable_exclusion_is_distinct_from_unknown_exposure(flags, reason):
    excluded, unknown = _api(**flags), _api("/unknown", api_kind="UNKNOWN_ROUTE")
    classify([excluded, unknown])
    assert excluded["eligibility"] == "EXCLUDED"
    assert excluded["eligibility_reasons"] == [reason]
    assert unknown["eligibility"] == "UNRESOLVED"


@pytest.mark.parametrize("issue", ["AMBIGUOUS_DYNAMIC_MATCH", "SOURCE_IDENTITY_COLLISION", "DUPLICATE_SOURCE_MAPPING"])
def test_ambiguous_identity_never_enters_strict_denominator(issue):
    record = _api(registry=True, enabled=["one"])
    record["issues"] = [issue]
    classify([record])
    assert record["eligibility"] == "UNRESOLVED"
    assert record["coverage_status"] == "UNRESOLVED"
    assert calculate_coverage([record])["static_case_coverage"]["denominator"] == 0


def test_methodless_source_cannot_be_guessed_by_override():
    record = _api(method="ANY")
    classify([record], {record["api_uid"]: {"resolution_status": "RESOLVED", "eligibility": "ELIGIBLE"}})
    assert record["eligibility"] == "UNRESOLVED"


@pytest.mark.parametrize("flags", [{"api_kind": "PAGE_ROUTE"}, {"deprecated": True}, {"internal": True}])
def test_reliable_exclusion_applies_even_to_methodless_mapping(flags):
    record = _api(method="ANY", **flags)
    classify([record])
    assert record["eligibility"] == "EXCLUDED"
    assert record["coverage_status"] == "EXCLUDED"


def test_unknown_noncontroller_candidate_does_not_prove_inactive_exposure():
    record = _api(api_kind="UNKNOWN_ROUTE", active=False)
    classify([record])
    assert record["eligibility"] == "UNRESOLVED"
    classify([record], {record["api_uid"]: {"active": False}})
    assert record["eligibility"] == "EXCLUDED"


def test_manual_exclusion_can_exclude_unresolved_identity():
    record = _api(method="ANY")
    classify([record], {record["api_uid"]: {"eligibility": "EXCLUDED"}})
    assert record["eligibility"] == "EXCLUDED"


@pytest.mark.parametrize("flag", ["deprecated", "internal", "active"])
def test_conflicting_source_classifications_remain_unresolved(flag):
    record = _api()
    duplicate = copy.deepcopy(record["source_records"][0])
    duplicate[flag] = not duplicate[flag]
    duplicate["line"] = 20
    record["source_records"].append(duplicate)
    record["issues"] = ["SOURCE_IDENTITY_COLLISION"]
    classify([record])
    assert record["eligibility"] == "UNRESOLVED"


def test_explicit_overrides_are_visible_and_can_resolve_a_collision():
    record = _api()
    record["issues"] = ["SOURCE_IDENTITY_COLLISION"]
    override = {"resolution_status": "RESOLVED", "business_risk": 30, "module_weight": 50}
    classify([record], {record["api_uid"]: override})
    assert record["eligibility"] == "ELIGIBLE"
    assert record["applied_override"] == override
    assert record["issues"] == ["SOURCE_IDENTITY_COLLISION"]


def test_manual_source_exposure_proof_can_classify_postman_only_api():
    record = _api(source=False)
    classify([record], {record["api_uid"]: {"api_kind": "HTTP_API", "active": True}})
    assert record["eligibility"] == "ELIGIBLE"


@pytest.mark.parametrize("override", [{"risk": 1}, {"business_risk": -1}, {"business_risk": float("nan")}, {"module_weight": True}, {"active": "true"}])
def test_invalid_overrides_fail_instead_of_silently_influencing_denominator(override):
    record = _api()
    with pytest.raises(ValueError):
        classify([record], {record["api_uid"]: override})


def test_legacy_dispatch_needs_explicit_business_exposure_proof():
    record = _api("/servlet/qdbAction", "POST", source=False)
    record["identity"] = make_identity("wizbank", "POST", "/servlet/qdbAction", dispatch_signature=["cmd=listUsers"])
    record["api_uid"] = api_uid(record["identity"])
    classify([record])
    assert record["eligibility"] == "UNRESOLVED"
    assert record["eligibility_reasons"] == ["LEGACY_EXPOSURE_UNPROVEN"]
    classify([record], {record["api_uid"]: {"active_business_exposure": True}})
    assert record["eligibility"] == "ELIGIBLE"
    assert record["applied_override"] == {"active_business_exposure": True}


def test_legacy_missing_dispatch_cannot_become_eligible_from_exposure_override():
    record = _api("/servlet/Dispatcher", "POST", source=False)
    record["identity"]["dispatch_signature"] = ["cmd=listUsers"]
    classify([record], {record["api_uid"]: {"active_business_exposure": True, "eligibility": "ELIGIBLE"}})
    assert record["eligibility"] == "UNRESOLVED"


def test_unique_api_metrics_and_disabled_case_exclusion():
    covered = _api(registry=True, enabled=["one", "two"], disabled=["disabled"])
    registered_untested = _api("/other", registry=True, disabled=["disabled-other"])
    source_only = _api("/source", postman=False)
    page = _api("/page", api_kind="PAGE_ROUTE", registry=True, enabled=["page-case"])
    records = classify([covered, registered_untested, source_only, page])
    summary = calculate_coverage(records + [copy.deepcopy(covered)])
    assert summary["counts"]["canonical_apis"] == 4
    assert summary["counts"]["eligible"] == 3
    assert summary["inventory_registration_coverage"]["value"] == pytest.approx(2 / 3)
    assert summary["static_case_coverage"]["numerator"] == 1
    assert summary["static_case_coverage"]["value"] == pytest.approx(1 / 3)
    assert summary["registry_utilization"]["value"] == pytest.approx(2 / 3)
    assert summary["counts"]["enabled_cases"] == 3
    assert summary["counts"]["disabled_cases"] == 2


def test_zero_denominator_has_structured_not_applicable_metric():
    summary = calculate_coverage([])
    for key in ("inventory_registration_coverage", "static_case_coverage", "registry_utilization"):
        assert summary[key] == {"numerator": 0, "denominator": 0, "value": None, "percentage": None, "status": "NOT_APPLICABLE"}


def test_registry_definition_without_resolved_endpoint_id_is_only_evidence():
    record = _api()
    record["registry_records"] = [{"origin": "registry", "file": "invalid.yaml", "endpoint_id": "", "resolution_status": "MISSING_ENDPOINT_ID"}]
    classify([record])
    summary = calculate_coverage([record])
    assert record["coverage_status"] == "SOURCE_POSTMAN_UNREGISTERED"
    assert summary["inventory_registration_coverage"]["numerator"] == 0
    assert summary["registry_utilization"]["denominator"] == 0
    assert summary["counts"]["registry_definitions"] == 1
    assert build_priority([record])[0]["coverage_gap"] == 50


def test_conflicting_classifications_for_same_uid_are_conservative():
    eligible = classify([_api()])[0]
    unresolved = copy.deepcopy(eligible)
    unresolved["eligibility"] = "UNRESOLVED"
    summary = calculate_coverage([eligible, unresolved])
    assert summary["counts"]["unresolved"] == 1
    assert summary["inventory_registration_coverage"]["denominator"] == 0


def test_priority_prefers_proven_readonly_gap_and_keeps_unconfigured_risk_zero():
    preferred, source_only, mutation = _api(), _api("/source", postman=False), _api("/mutation", "POST")
    queue = build_priority(classify([mutation, source_only, preferred]))
    assert queue[0]["api_uid"] == preferred["api_uid"]
    assert queue[-1]["api_uid"] == mutation["api_uid"]
    assert queue[-1]["safety_gate"] == "BLOCKED_FOR_IMPLEMENTATION"
    assert all(row["business_risk"] == 0 and row["business_risk_source"] == "UNCONFIGURED" for row in queue)
    assert all(key in queue[0] for key in ("coverage_gap", "evidence_confidence", "business_risk", "change_risk", "automation_feasibility", "total", "safety_gate"))


def test_mutation_requires_safe_lifecycle_and_destructive_get_stays_blocked():
    mutation = _api(method="PUT")
    mutation["case_records"] = [{"case_id": "disabled-design", "enabled": False, "safe_lifecycle": True}]
    destructive_get = _api("/danger", registry=True)
    destructive_get["registry_records"][0]["destructive"] = True
    classify([mutation, destructive_get])
    queue = build_priority([mutation, destructive_get], {destructive_get["api_uid"]: {"readonly": True}})
    by_uid = {row["api_uid"]: row for row in queue}
    assert by_uid[mutation["api_uid"]]["safety_gate"] == "LIFECYCLE_REVIEW_REQUIRED"
    assert by_uid[destructive_get["api_uid"]]["safety_gate"] == "BLOCKED_FOR_IMPLEMENTATION"


@pytest.mark.parametrize("override", [{}, {"readonly": True}])
def test_source_declared_get_mutation_is_blocked_without_inventing_business_risk(override):
    mutation = _api("/ordinary", java_method="delExamine", mutation_candidate=True,
                    mutation_evidence="JAVA_OPERATION_DECLARATION")
    readonly = _api("/delete-looking-path")
    records = classify([mutation, readonly])
    queue = build_priority(records, {mutation["api_uid"]: override})
    by_uid = {row["api_uid"]: row for row in queue}
    assert by_uid[mutation["api_uid"]]["readonly"] is False
    assert by_uid[mutation["api_uid"]]["safety_gate"] == "BLOCKED_FOR_IMPLEMENTATION"
    assert by_uid[mutation["api_uid"]]["business_risk"] == 0
    assert by_uid[mutation["api_uid"]]["business_risk_source"] == "UNCONFIGURED"
    assert by_uid[readonly["api_uid"]]["safety_gate"] == "REVIEW_REQUIRED"
    assert calculate_coverage(records)["static_case_coverage"]["denominator"] == 2


def test_configured_risk_has_visible_weight_and_does_not_remove_safety_gate():
    mutation = _api(method="DELETE")
    override = {"business_risk": 80, "module_weight": 50}
    classify([mutation], {mutation["api_uid"]: override})
    row = build_priority([mutation])[0]
    assert row["business_risk"] == 80
    assert row["business_risk_source"] == "OVERRIDE"
    assert row["module_weight"] == 50
    assert row["total"] == 50 + 25 + 40 + 0 + 2
    assert row["safety_gate"] == "BLOCKED_FOR_IMPLEMENTATION"


def test_covered_and_excluded_apis_are_absent_from_priority_queue():
    covered, excluded = _api(enabled=["case"]), _api("/page", api_kind="PAGE_ROUTE")
    assert build_priority(classify([covered, excluded])) == []


def _reports(output, records, issues=(), duplicates=(), metadata=None):
    return write_reports(output, records, calculate_coverage(records), list(issues), list(duplicates), build_priority(records), metadata or {})


def test_all_reports_are_byte_stable_under_input_and_evidence_reordering(tmp_path):
    records = classify([_api("/中文"), _api("/users", registry=True, disabled=["two", "one"])])
    records[0]["postman_records"].append({**records[0]["postman_records"][0], "file": "another/request.yaml", "request_name": "Duplicate evidence"})
    issues = [{"category": "AMBIGUOUS_METHOD", "file": "Z.java"}, {"code": "MISSING_ENDPOINT_REFERENCE", "case_id": "missing", "file": "case.yaml"}]
    duplicates = [{"category": "DUPLICATE_REGISTRY", "api_uid": records[0]["api_uid"], "count": 2, "files": ["z.yaml", "a.yaml"]}]
    first = _reports(tmp_path / "first", records, issues, duplicates, {"scanner_limitations": ["No inherited mappings", "No Java execution"], "counts": {"source_files": 2}})
    shuffled = copy.deepcopy(records[::-1])
    shuffled[0]["disabled_case_ids"].reverse()
    shuffled[0]["runtime_paths"].reverse()
    shuffled[1]["postman_records"].reverse()
    second = _reports(tmp_path / "second", shuffled, issues[::-1], duplicates, {"scanner_limitations": ["No Java execution", "No inherited mappings"], "counts": {"source_files": 2}})
    expected = {"api_inventory.json", "api_inventory.csv", "coverage_summary.json", "unresolved.csv", "duplicates.csv", "api_inventory_report.md", "priority_queue.csv"}
    assert {Path(path).name for path in first} == expected
    assert all(Path(left).read_bytes() == Path(right).read_bytes() for left, right in zip(first, second))
    assert "中文" in (tmp_path / "first/api_inventory.json").read_text(encoding="utf-8")
    markdown = (tmp_path / "first/api_inventory_report.md").read_text(encoding="utf-8")
    for section in ("Summary", "Coverage", "Coverage Gap", "Legacy", "Excluded", "Unresolved", "Duplicates", "Priority Queue", "Scanner Limitations"):
        assert "## " + section in markdown
    assert "execution permission" in markdown


def test_report_projection_removes_canary_secrets_and_raw_requests(tmp_path):
    canaries = ["CANARY_AUTH_719", "CANARY_COOKIE_843", "CANARY_BEARER_282", "CANARY_QUERY_731", "CANARY_PASSWORD_024", "CANARY_CLIENT_632"]
    record = _api()
    record["postman_records"][0].update({
        "headers": {"Authorization": canaries[0], "Cookie": canaries[1]},
        "authorization": {"bearer": canaries[2]}, "query": {"page": canaries[3]},
        "body": {"password": canaries[4], "client_secret": canaries[5]},
        "raw_request": {"url": "https://example.invalid/?password=" + canaries[4]},
    })
    record["password"] = canaries[4]
    classify([record])
    paths = _reports(tmp_path, [record], [{"category": "EXAMPLE", "Authorization": canaries[0], "body": {"password": canaries[4]}}], metadata={"secret": canaries[5], "credentials": {"password": canaries[4]}})
    for path in paths:
        content = Path(path).read_text(encoding="utf-8")
        assert all(canary not in content for canary in canaries)
        assert "raw_request" not in content
    inventory = json.loads((tmp_path / "api_inventory.json").read_text(encoding="utf-8"))
    evidence = inventory["apis"][0]["postman_records"][0]
    assert evidence["header_keys"] == ["Authorization", "Cookie"]
    assert evidence["body_keys"] == ["client_secret", "password"]
    assert "headers" not in evidence and "body" not in evidence


def test_report_retains_safe_registry_and_lifecycle_proof_metadata(tmp_path):
    record = _api(registry=True, mutation_candidate=True, mutation_evidence="JAVA_OPERATION_DECLARATION")
    record["registry_records"][0].update({"host_class": "HOST_KEY", "auth_required": True})
    lifecycle = {"verify": "verify-id", "rollback": "rollback-id", "verify_rollback": "restore-check-id"}
    record["case_records"] = [{"origin": "case", "case_id": "mutation", "enabled": False, "lifecycle": lifecycle, "safe_lifecycle": False}]
    classify([record])
    _reports(tmp_path, [record])
    api = json.loads((tmp_path / "api_inventory.json").read_text(encoding="utf-8"))["apis"][0]
    assert api["registry_records"][0]["host_class"] == "HOST_KEY"
    assert api["registry_records"][0]["auth_required"] is True
    assert api["case_records"][0]["lifecycle"] == lifecycle
    assert api["source_records"][0]["mutation_candidate"] is True
    assert api["source_records"][0]["mutation_evidence"] == "JAVA_OPERATION_DECLARATION"


def test_unresolved_and_duplicate_reports_preserve_safe_locations_and_overrides(tmp_path):
    record = _api(source=False)
    override = {"business_risk": 20}
    classify([record], {record["api_uid"]: override})
    duplicates = [{"category": "DUPLICATE_REGISTRY", "api_uid": record["api_uid"], "count": 2, "files": ["b.yaml", "a.yaml"]}]
    _reports(tmp_path, [record], [{"code": "MISSING_ENDPOINT_REFERENCE", "file": "missing.yaml", "case_id": "missing"}], duplicates)
    with (tmp_path / "unresolved.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["record_type"] for row in rows} == {"UNRESOLVED_API", "ANALYZER_NOTICE"}
    assert any(row["file"] == "missing.yaml" for row in rows)
    with (tmp_path / "duplicates.csv").open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert json.loads(row["files"]) == ["a.yaml", "b.yaml"]
    inventory = json.loads((tmp_path / "api_inventory.json").read_text(encoding="utf-8"))
    assert inventory["apis"][0]["applied_override"] == override
    assert inventory["metadata"]["overrides"] == {record["api_uid"]: override}


def test_zero_denominator_markdown_shows_na_and_machine_report_stays_null(tmp_path):
    _reports(tmp_path, [])
    report = (tmp_path / "api_inventory_report.md").read_text(encoding="utf-8")
    assert report.count("N/A") == 3
    summary = json.loads((tmp_path / "coverage_summary.json").read_text(encoding="utf-8"))
    assert summary["static_case_coverage"]["value"] is None


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_existing_report_filename_links_cannot_mutate_other_assets(tmp_path, link_kind):
    import os

    original = tmp_path / "frozen-runtime.yaml"
    original.write_text("runtime: unchanged\n", encoding="utf-8")
    output = tmp_path / "reports"
    output.mkdir()
    names = ("api_inventory.json", "api_inventory.csv", "coverage_summary.json", "unresolved.csv", "duplicates.csv", "api_inventory_report.md", "priority_queue.csv")
    for name in names:
        if link_kind == "symlink":
            (output / name).symlink_to(original)
        else:
            os.link(original, output / name)
    _reports(output, classify([_api()]))
    assert original.read_text(encoding="utf-8") == "runtime: unchanged\n"
    assert all(not (output / name).is_symlink() for name in names)
    assert not list(output.glob(".report-*"))
