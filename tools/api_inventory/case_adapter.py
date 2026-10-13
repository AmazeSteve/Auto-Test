"""Read enabled and disabled raw YAML with frozen effective-request semantics."""

from collections import defaultdict

from .models import ScanResult
from .registry_adapter import _identify_request, _read_yaml_records


_LIFECYCLE_ROLES = ("verify", "rollback", "verify_rollback")


def _merge_case_with_endpoint(case, endpoint):
    """Independent copy of the frozen pure merge; no runtime package imports.

    Case values override complete endpoint fields, except headers merge deeply
    and tags combine endpoint defaults, destructive marker, then case tags.
    Params/data/json remain shallow replacements. Values exist only privately.
    """
    endpoint_id = case.get("endpoint")
    merged = endpoint.copy()
    merged.pop("_file", None)
    merged.pop("default_tags", None)
    for key, value in case.items():
        if key != "endpoint":
            merged[key] = value
    endpoint_tags = list(endpoint.get("default_tags", []))
    case_tags = list(case.get("tags", []))
    if endpoint.get("destructive") is True:
        endpoint_tags.append("destructive")
    combined_tags = []
    seen = set()
    for tag in endpoint_tags + case_tags:
        if tag not in seen:
            combined_tags.append(tag)
            seen.add(tag)
    merged["tags"] = combined_tags
    endpoint_headers = endpoint.get("headers", {}) or {}
    case_headers = case.get("headers", {})
    if endpoint_headers or case_headers:
        merged_headers = {}
        merged_headers.update(endpoint_headers)
        merged_headers.update(case_headers)
        merged["headers"] = merged_headers
    if "name" not in case and "name" in endpoint:
        merged["name"] = endpoint["name"]
    if "module" not in case and "module" in endpoint:
        merged["module"] = endpoint["module"]
    if endpoint_id:
        merged["_endpoint_id"] = endpoint_id
    return merged


def _enabled(case):
    # Literal False and enabled-over-enable precedence match frozen CaseLoader.
    return case.get("enabled", case.get("enable", True)) is not False


def _tags(case):
    values = case.get("tags", []) or []
    return sorted({value for value in values if isinstance(value, str)}) if isinstance(values, list) else []


def _destructive(case):
    return case.get("destructive") is True or "destructive" in _tags(case)


def _depends(case):
    values = case.get("depends_on", []) or []
    return [values] if isinstance(values, str) else values if isinstance(values, list) else []


def _proven_lifecycle(root, case_map, child_owners, resolved_endpoints):
    """Prove a complete static recovery contract, never successful execution."""
    root_id = root.get("id")
    lifecycle = root.get("lifecycle")
    if (not _enabled(root) or not _destructive(root) or not isinstance(lifecycle, dict)
            or set(lifecycle) != set(_LIFECYCLE_ROLES)):
        return False
    ids = list(lifecycle.values())
    if (not all(isinstance(value, str) and value for value in ids)
            or root_id in ids or len(set(ids)) != 3):
        return False
    children = {}
    for role in _LIFECYCLE_ROLES:
        child_id = lifecycle[role]
        candidates = case_map.get(child_id, [])
        if len(candidates) != 1 or child_owners.get(child_id) != {root_id}:
            return False
        child = candidates[0]
        if not _enabled(child) or child.get("lifecycle") or not child.get("_analyzer_resolved"):
            return False
        children[role] = child
    effect = root.get("effect")
    capture = effect.get("capture") if isinstance(effect, dict) else None
    if (not isinstance(capture, dict) or capture.get("enabled", True) is False
            or not isinstance(capture.get("before"), dict) or not isinstance(capture.get("after"), dict)):
        return False
    capture_ids = [capture[stage].get("endpoint") for stage in ("before", "after")]
    if not all(isinstance(endpoint_id, str) and endpoint_id in resolved_endpoints for endpoint_id in capture_ids):
        return False
    verify, rollback, verify_rollback = (children[role] for role in _LIFECYCLE_ROLES)
    if (root_id not in _depends(verify) or "verify" not in _tags(verify)
            or root_id not in _depends(rollback) or "rollback" not in _tags(rollback)
            or rollback.get("destructive") is not True
            or lifecycle["rollback"] not in _depends(verify_rollback)
            or not {"rollback", "verify"}.issubset(_tags(verify_rollback))):
        return False
    mechanisms = [key for key in ("restore_mapping", "restore_payload") if key in rollback]
    if len(mechanisms) != 1:
        return False
    mechanism = mechanisms[0]
    config = rollback[mechanism]
    if not isinstance(config, dict) or config.get("source_effect") != root_id:
        return False
    if mechanism == "restore_payload":
        if set(config) != {"source_effect"} or not isinstance(root.get("request_builder"), dict):
            return False
    elif not isinstance(config.get("fields"), dict) or not config["fields"]:
        return False
    return True


def scan_cases(repo_root, registry_records, service="wizbank"):
    """Effective identity follows case overrides; disabled cases remain evidence."""
    if isinstance(registry_records, ScanResult):
        registry_records = registry_records.records
    result = ScanResult(counts={"enabled_cases": 0, "disabled_cases": 0})
    endpoints = defaultdict(list)
    for _, _, endpoint in _read_yaml_records(repo_root, "data/endpoints"):
        endpoint_id = endpoint.get("id")
        if isinstance(endpoint_id, str) and endpoint_id:
            endpoints[endpoint_id].append(endpoint)
    registry_by_id = defaultdict(list)
    for record in registry_records:
        if record.get("endpoint_id"):
            registry_by_id[record["endpoint_id"]].append(record)
    effective_cases = []
    case_map = defaultdict(list)
    for relative, index, case in _read_yaml_records(repo_root, "data/cases"):
        enabled = _enabled(case)
        result.counts["enabled_cases" if enabled else "disabled_cases"] += 1
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            case_id = ""
            result.issues.append({"code": "MISSING_CASE_ID", "origin": "case", "file": relative, "index": index})
        endpoint_id = case.get("endpoint")
        record = {"origin": "case", "case_id": case_id, "file": relative, "index": index,
                  "enabled": enabled, "endpoint_id": endpoint_id if isinstance(endpoint_id, str) else "",
                  "endpoint_override": False, "safe_lifecycle": False}
        candidates = endpoints.get(endpoint_id, []) if isinstance(endpoint_id, str) else []
        reference_issue = None
        if endpoint_id:
            if not candidates:
                reference_issue = "MISSING_ENDPOINT_REFERENCE"
            elif len(candidates) != 1 or len(registry_by_id.get(endpoint_id, [])) != 1:
                reference_issue = "AMBIGUOUS_ENDPOINT_REFERENCE"
        try:
            effective = _merge_case_with_endpoint(case, candidates[0]) if endpoint_id and not reference_issue else case.copy()
        except (TypeError, ValueError):
            raise ValueError(f"Invalid framework merge fields: {relative}, item {index}") from None
        record["destructive"] = _destructive(effective)
        record["tags"] = _tags(effective)
        lifecycle = effective.get("lifecycle")
        if isinstance(lifecycle, dict):
            record["lifecycle"] = {role: lifecycle[role] for role in _LIFECYCLE_ROLES
                                   if isinstance(lifecycle.get(role), str) and lifecycle[role]}
        _identify_request(record, effective, service, result.issues)
        if reference_issue:
            record["resolution_status"] = reference_issue
            for key in ("api_uid", "identity", "display_identity"):
                record.pop(key, None)
            result.issues.append({"code": reference_issue, "origin": "case", "file": relative,
                                  "case_id": case_id, "endpoint_id": record["endpoint_id"]})
        elif endpoint_id:
            endpoint_record = registry_by_id[endpoint_id][0]
            endpoint_uid = endpoint_record.get("api_uid")
            if endpoint_uid:
                record["endpoint_api_uid"] = endpoint_uid
            if endpoint_uid and record.get("api_uid") and endpoint_uid != record["api_uid"]:
                record["endpoint_override"] = True
                result.issues.append({"code": "ENDPOINT_IDENTITY_OVERRIDE", "origin": "case", "file": relative,
                                      "case_id": case_id, "endpoint_id": endpoint_id,
                                      "endpoint_api_uid": endpoint_uid, "effective_api_uid": record["api_uid"]})
        if not case_id:
            record["resolution_status"] = "MISSING_CASE_ID"
        effective["_analyzer_resolved"] = bool(record.get("api_uid")) and record["resolution_status"] == "RESOLVED"
        result.records.append(record)
        effective_cases.append((record, effective))
        if case_id:
            case_map[case_id].append(effective)
    child_owners = defaultdict(set)
    for record, case in effective_cases:
        lifecycle = case.get("lifecycle")
        if isinstance(lifecycle, dict):
            for child_id in lifecycle.values():
                if isinstance(child_id, str):
                    child_owners[child_id].add(record["case_id"])
    resolved_endpoints = {endpoint_id for endpoint_id, records in registry_by_id.items()
                          if len(records) == 1 and len(endpoints.get(endpoint_id, [])) == 1
                          and records[0].get("api_uid") and records[0].get("resolution_status") == "RESOLVED"}
    for record, effective in effective_cases:
        record["safe_lifecycle"] = (_proven_lifecycle(effective, case_map, child_owners, resolved_endpoints)
                                    if record.get("api_uid") and len(case_map.get(record["case_id"], [])) == 1 else False)
    for case_id, cases in sorted(case_map.items()):
        if len(cases) > 1:
            result.issues.append({"code": "DUPLICATE_CASE_ID", "origin": "case", "case_id": case_id, "count": len(cases)})
    result.limitations.append("Safe lifecycle indicates a complete static recovery contract; it does not establish execution or successful restoration.")
    return result
