"""Identity boundaries and conservative evidence matching are denominator gates."""

from copy import deepcopy

import pytest

from tools.api_inventory.identity import api_uid, identify, make_identity
from tools.api_inventory.models import ScanResult
from tools.api_inventory.path_normalizer import normalize_path
from tools.api_inventory.reconciler import reconcile


@pytest.mark.parametrize("path", ["/app/v1/users/", "/app//v1/users?token=private#x",
                                  "https://private.example/app/v1/users?password=private",
                                  "HTTPS://user:private@host.invalid/app/v1/users",
                                  "//user:private@host.invalid/app/v1/users",
                                  "{{Host-be}}/app/v1/users", "{{host}}app/v1/users",
                                  "{{Local_Host_SiYuan}}/app/v1/users"])
def test_runtime_context_and_host_do_not_change_identity(path):
    assert make_identity("wizbank", "GET", path) == make_identity("wizbank", "GET", "/v1/users")


@pytest.mark.parametrize("placeholder", ["{first}", "{different}", "{{id}}", "${id}"])
def test_explicit_placeholder_names_have_no_identity_significance(placeholder):
    assert make_identity("wizbank", "GET", "/v1/users/" + placeholder)["route_fingerprint"] == "/v1/users/{}"


def test_concrete_segments_stay_concrete_and_prefix_boundary_is_exact():
    assert normalize_path("///app///users//")["canonical_path"] == "/users"
    assert normalize_path("/application/users")["canonical_path"] == "/application/users"
    assert normalize_path("/app/users", "other")["canonical_path"] == "/app/users"
    identity = make_identity("wizbank", "GET", "/v1/users/123/3fafaf12-1234")
    assert identity["route_fingerprint"] == "/v1/users/123/3fafaf12-1234"


def test_full_uid_and_condition_semantics_are_deterministic():
    left = make_identity("wizbank", "post", "/x", {"params": [" b ", "a", "b"], "headers": ["X=one"]},
                         ["module=m", "cmd=c", "cmd=c"])
    right = make_identity("wizbank", "POST", "/x", {"headers": ["X=one"], "params": ["a", "b"]},
                          ["cmd=c", "module=m"])
    assert left == right
    assert api_uid(left) == api_uid(right)
    assert len(api_uid(left)) == 64
    assert api_uid(left) != api_uid(make_identity("wizbank", "GET", "/x"))
    assert api_uid(left) != api_uid(make_identity("wizbank", "POST", "/x", {"headers": ["X=two"]}))


def test_sensitive_condition_comparisons_have_secret_safe_stable_identity():
    conditions = {"headers": ["Authorization=CANARY_BEARER", "Cookie!=CANARY_COOKIE"],
                  "params": ["password=CANARY_PASSWORD", "mode=business"]}
    identity = make_identity("wizbank", "GET", "/x", conditions)
    assert "CANARY_" not in str(identity)
    assert identity["mapping_conditions"]["params"][0] == "mode=business"
    assert "Cookie!=sha256:" in identity["mapping_conditions"]["headers"][1]
    assert identity == make_identity("wizbank", "GET", "/x", conditions)
    assert api_uid(identity) != api_uid(make_identity("wizbank", "GET", "/x", identity["mapping_conditions"]))
    changed = make_identity("wizbank", "GET", "/x", {"headers": ["Authorization=different"]})
    assert api_uid(changed) != api_uid(identity)


@pytest.mark.parametrize("conditions,signature", [({"surprise": []}, []), ({"headers": [4]}, []), ({}, "cmd=x")])
def test_noncanonical_values_are_rejected(conditions, signature):
    with pytest.raises(ValueError):
        make_identity("wizbank", "GET", "/x", conditions, signature)


def evidence(origin, path, method="GET", **extra):
    record = {"origin": origin, "file": origin + ".fixture", "method": method,
              "runtime_path": path, "resolution_status": "RESOLVED", **extra}
    if origin == "source":
        record.update({"line": extra.get("line", 1), "java_method": "read", "api_kind": "HTTP_API",
                       "active": True, "deprecated": False, "internal": False})
    if origin == "registry":
        record["endpoint_id"] = extra.get("endpoint_id", "registered")
    if origin == "case":
        record.update({"case_id": extra.get("case_id", "case"), "enabled": extra.get("enabled", True)})
    return identify(record)


def test_exact_merge_preserves_multiple_evidence_and_inputs():
    scan = ScanResult(records=[evidence("source", "/v1/u"), evidence("postman", "/app/v1/u"),
                               evidence("postman", "/v1/u"), evidence("registry", "/v1/u"),
                               evidence("case", "/v1/u"), evidence("case", "/v1/u", case_id="disabled", enabled=False)])
    original = deepcopy(scan)
    records, issues, duplicates = reconcile(scan)
    assert scan == original
    assert len(records) == 1 and not issues
    assert records[0]["confidence"] == "HIGH"
    assert len(records[0]["postman_records"]) == 2
    assert records[0]["enabled_case_ids"] == ["case"]
    assert records[0]["disabled_case_ids"] == ["disabled"]
    assert duplicates[0]["category"] == "DUPLICATE_POSTMAN_EVIDENCE"


def test_unique_source_template_matches_concrete_route_without_number_inference():
    source = evidence("source", "/v1/users/{id}")
    postman = evidence("postman", "/app/v1/users/arbitrary-name")
    records, issues, _ = reconcile(ScanResult(records=[source, postman]))
    assert len(records) == 1 and not issues
    assert records[0]["api_uid"] == source["api_uid"]
    assert records[0]["postman_records"][0]["original_api_uid"] == postman["api_uid"]
    assert records[0]["postman_records"][0]["runtime_path"] == "/app/v1/users/arbitrary-name"


def test_multiple_dynamic_templates_are_unresolved_not_arbitrarily_chosen():
    records, issues, _ = reconcile(ScanResult(records=[evidence("source", "/x/{id}"),
                                                     evidence("source", "/{group}/y"), evidence("postman", "/x/y")]))
    concrete = next(record for record in records if record["postman_records"])
    assert concrete["resolution_status"] == "UNRESOLVED"
    assert "AMBIGUOUS_DYNAMIC_MATCH" in concrete["issues"]
    assert len(issues[0]["candidate_api_uids"]) == 2


def test_dynamic_matching_keeps_method_fixed_segments_and_conditions():
    source = evidence("source", "/x/{id}", mapping_conditions={"params": ["mode=one"]})
    records, _, _ = reconcile(ScanResult(records=[source, evidence("postman", "/x/a"),
                                                 evidence("postman", "/other/a"), evidence("postman", "/x/a", "POST")]))
    assert len(records) == 4
    assert not next(record for record in records if record["source_records"])["postman_records"]


def test_source_collision_and_duplicate_registry_are_reported():
    records, _, duplicates = reconcile(ScanResult(records=[evidence("source", "/x", line=1),
                                                           evidence("source", "/x", line=10),
                                                           evidence("registry", "/x", endpoint_id="one"),
                                                           evidence("registry", "/x", endpoint_id="two")]))
    assert records[0]["resolution_status"] == "UNRESOLVED"
    assert {duplicate["category"] for duplicate in duplicates} == {"SOURCE_IDENTITY_COLLISION", "DUPLICATE_REGISTRY"}


def test_invalid_case_and_registry_ids_do_not_claim_static_coverage():
    from tools.api_inventory.classifier import classify
    from tools.api_inventory.coverage import calculate_coverage

    records, _, _ = reconcile(ScanResult(records=[evidence("source", "/x"),
                                                 evidence("registry", "/x", endpoint_id="", resolution_status="MISSING_ENDPOINT_ID"),
                                                 evidence("case", "/x", case_id="", resolution_status="MISSING_CASE_ID")]))
    assert records[0]["enabled_case_ids"] == []
    assert records[0]["endpoint_ids"] == []
    assert classify(records)[0]["eligibility"] == "ELIGIBLE"
    summary = calculate_coverage(records)
    assert summary["static_case_coverage"]["numerator"] == 0
    assert summary["inventory_registration_coverage"]["numerator"] == 0
