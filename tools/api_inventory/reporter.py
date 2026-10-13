"""Deterministic UTF-8 reports with explicit safe-field projections."""

import csv
import json
import os
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .classifier import get_override, is_legacy
from .models import stable_json


METRICS = (
    ("inventory_registration_coverage", "Inventory Registration Coverage"),
    ("static_case_coverage", "Static Case Coverage"),
    ("registry_utilization", "Registry Utilization"),
)
EVIDENCE_SCALARS = frozenset({
    "origin", "file", "line", "index", "controller", "java_method", "mapping_annotation", "request_name",
    "method", "runtime_path", "canonical_path", "route_fingerprint", "host_form",
    "api_uid", "display_identity", "api_kind", "active", "deprecated", "internal",
    "resolution_status", "body_type", "endpoint_id", "case_id", "module",
    "auth_type", "auth_present", "host_class", "auth_required", "enabled", "destructive", "safe_lifecycle",
    "endpoint_override", "endpoint_api_uid", "original_api_uid", "reconciliation",
    "mutation_candidate", "mutation_evidence",
})
EVIDENCE_LISTS = frozenset({"query_keys", "header_keys", "body_keys", "tags", "unresolved_attributes"})
ISSUE_SCALARS = frozenset({
    "code", "category", "origin", "file", "line", "index", "api_uid",
    "endpoint_id", "case_id", "endpoint_api_uid", "effective_api_uid",
    "request_name", "controller", "java_method", "count", "reason", "attribute", "mapping_annotation",
})
ISSUE_LISTS = frozenset({"files", "endpoint_ids", "candidate_api_uids", "case_ids", "unresolved_attributes"})
INVENTORY_COLUMNS = (
    "api_uid", "display_identity", "service", "method", "route_fingerprint",
    "mapping_conditions", "dispatch_signature", "eligibility", "coverage_eligible",
    "eligibility_reasons", "coverage_status", "confidence", "resolution_status",
    "source_evidence_count", "postman_evidence_count", "endpoint_ids",
    "enabled_case_ids", "disabled_case_ids", "runtime_paths", "issues", "applied_override",
)
PRIORITY_COLUMNS = (
    "api_uid", "display_identity", "method", "eligibility", "coverage_status",
    "readonly", "coverage_gap", "evidence_confidence", "business_risk",
    "business_risk_source", "module_weight", "change_risk", "automation_feasibility",
    "total", "safety_gate",
)


def _scalar(value):
    return value is None or isinstance(value, (str, bool, int, float))


def _strings(values):
    return sorted({value for value in values if isinstance(value, str)}) if isinstance(values, (list, tuple, set)) else []


def _identity(identity):
    return {
        **{key: identity[key] for key in ("service", "method", "route_fingerprint") if key in identity and _scalar(identity[key])},
        "mapping_conditions": {key: _strings(identity.get("mapping_conditions", {}).get(key, [])) for key in ("params", "headers", "consumes", "produces")},
        "dispatch_signature": _strings(identity.get("dispatch_signature", [])),
    }


def _evidence(evidence):
    projected = {key: value for key, value in evidence.items() if key in EVIDENCE_SCALARS and _scalar(value)}
    projected.update({key: _strings(evidence[key]) for key in EVIDENCE_LISTS if key in evidence})
    if isinstance(evidence.get("identity"), dict):
        projected["identity"] = _identity(evidence["identity"])
    if isinstance(evidence.get("mapping_conditions"), dict):
        projected["mapping_conditions"] = _identity(evidence)["mapping_conditions"]
    if "dispatch_signature" in evidence:
        projected["dispatch_signature"] = _strings(evidence["dispatch_signature"])
    if isinstance(evidence.get("lifecycle"), dict):
        projected["lifecycle"] = {key: evidence["lifecycle"][key] for key in ("verify", "rollback", "verify_rollback") if key in evidence["lifecycle"] and isinstance(evidence["lifecycle"][key], str)}
    return projected


def _issue(issue):
    if isinstance(issue, str):
        return {"category": issue}
    projected = {key: value for key, value in issue.items() if key in ISSUE_SCALARS and _scalar(value)}
    projected.update({key: _strings(issue[key]) for key in ISSUE_LISTS if key in issue})
    return projected


def _record(record):
    projected = {key: record[key] for key in ("api_uid", "display_identity", "eligibility", "coverage_eligible", "coverage_status", "confidence", "resolution_status") if key in record and _scalar(record[key])}
    projected["identity"] = _identity(record.get("identity", {}))
    for key in ("runtime_paths", "canonical_paths", "endpoint_ids", "registry_files", "enabled_case_ids", "disabled_case_ids", "eligibility_reasons"):
        projected[key] = _strings(record.get(key, []))
    projected["issues"] = sorted((_issue(issue) for issue in record.get("issues", [])), key=stable_json)
    projected["applied_override"] = get_override(record, {record["api_uid"]: record.get("applied_override", {})})
    for key in ("source_records", "postman_records", "registry_records", "case_records"):
        projected[key] = sorted((_evidence(evidence) for evidence in record.get(key, [])), key=stable_json)
    return projected


def _counts(counts):
    return {str(key): value for key, value in sorted(counts.items()) if isinstance(value, int) and not isinstance(value, bool) and value >= 0}


def _summary(summary):
    projected = {"counts": _counts(summary.get("counts", {})), "coverage_status_counts": _counts(summary.get("coverage_status_counts", {}))}
    for key, _ in METRICS:
        metric = summary[key]
        projected[key] = {field: metric[field] for field in ("numerator", "denominator", "value", "percentage", "status")}
    return projected


def _metadata(metadata, records):
    projected = {"counts": _counts(metadata.get("counts", {})),
                 "scanner_limitations": _strings(metadata.get("scanner_limitations", metadata.get("limitations", [])))}
    overrides = {record["api_uid"]: record["applied_override"] for record in records if record.get("applied_override")}
    # Also retain configured overrides for unmatched UIDs so configuration is auditable.
    for uid, override in metadata.get("overrides", {}).items():
        overrides[uid] = get_override({"api_uid": uid}, {uid: override})
    projected["overrides"] = dict(sorted(overrides.items()))
    return projected


def _inventory_row(record):
    identity = record["identity"]
    row = {key: record.get(key, "") for key in INVENTORY_COLUMNS if key in record}
    row.update({key: identity.get(key, "") for key in ("service", "method", "route_fingerprint", "mapping_conditions", "dispatch_signature")})
    row["source_evidence_count"] = len(record["source_records"])
    row["postman_evidence_count"] = len(record["postman_records"])
    return row


@contextmanager
def _atomic_file(path):
    """Replace existing links, never write through them into another asset."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, prefix=".report-", delete=False) as stream:
            temporary = Path(stream.name)
            yield stream
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_csv(path, columns, rows):
    with _atomic_file(path) as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: stable_json(value) if isinstance(value, (dict, list)) else value for key, value in row.items() if key in columns})


def _write_json(path, value):
    with _atomic_file(path) as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")


def _cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _table(headers, rows):
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"] + ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]


def _api_table(records):
    if not records:
        return ["None."]
    rows = [(record["display_identity"], record["eligibility"], record["coverage_status"]) for record in records[:100]]
    return _table(("API", "Eligibility", "Static status"), rows) + ([f"Showing 100 of {len(records)}; all records are in api_inventory.json."] if len(records) > 100 else [])


def _markdown(records, summary, issues, duplicates, priority, metadata):
    counts = {**summary["counts"], **metadata["counts"]}
    lines = ["# API Inventory & Static Coverage", "", "## Summary", "", "This offline analysis describes static evidence. Case coverage does not assert API execution or a passing result.", ""]
    lines += _table(("Count", "Value"), sorted(counts.items()))
    lines += ["", "## Coverage", ""]
    rows = []
    for key, label in METRICS:
        metric = summary[key]
        percentage = "N/A" if metric["percentage"] is None else f'{metric["percentage"]:.2f}%'
        rows.append((label, metric["numerator"], metric["denominator"], percentage))
    lines += _table(("Metric", "Numerator", "Denominator", "Coverage"), rows)
    lines += ["", "The strict denominator contains unique ELIGIBLE canonical APIs. Registry Utilization measures registered APIs with enabled cases independently of source exposure eligibility."]
    sections = (
        ("Coverage Gap", [record for record in records if record["eligibility"] == "ELIGIBLE" and not record["enabled_case_ids"]]),
        ("Legacy", [record for record in records if is_legacy(record)]),
        ("Excluded", [record for record in records if record["eligibility"] == "EXCLUDED"]),
        ("Unresolved", [record for record in records if record["eligibility"] == "UNRESOLVED"]),
    )
    for title, selected in sections:
        lines += ["", "## " + title, ""] + _api_table(selected)
        if title == "Unresolved" and issues:
            categories = Counter(issue.get("category", issue.get("code", "UNSPECIFIED")) for issue in issues)
            lines += ["", "Scanner and adapter notices:", ""] + _table(("Category", "Count"), sorted(categories.items()))
    lines += ["", "## Duplicates", ""]
    lines += _table(("Category", "API UID", "Evidence count"), [(issue.get("category", issue.get("code", "")), issue.get("api_uid", ""), issue.get("count", "")) for issue in duplicates]) if duplicates else ["None."]
    lines += ["", "## Priority Queue", "", "Scores recommend case-design review; they never grant execution permission. Mutations without proven safe lifecycle evidence are blocked for implementation.", ""]
    lines += _table(("API", "Total", "Safety gate", "Business risk source"), [(row.get("display_identity", ""), row.get("total", ""), row.get("safety_gate", ""), row.get("business_risk_source", "")) for row in priority[:50]]) if priority else ["None."]
    if len(priority) > 50:
        lines += [f"Showing 50 of {len(priority)}; all recommendations are in priority_queue.csv."]
    lines += ["", "## Scanner Limitations", ""]
    lines += ["- " + limitation for limitation in metadata["scanner_limitations"]] or ["No additional limitations supplied."]
    lines += ["", "Manual analyzer overrides are retained in inventory metadata and each affected API record.", ""]
    return "\n".join(lines)


def write_reports(output_dir, records, summary, issues, duplicates, priority, metadata):
    """Write fixed report names. Unknown/raw request fields are never serialized."""
    output = Path(output_dir)
    if output.is_symlink():
        raise ValueError("Report output directory cannot be a symbolic link")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise ValueError("Report output must be a directory")
    safe_records = sorted((_record(record) for record in records), key=lambda record: record["api_uid"])
    safe_issues = sorted((_issue(issue) for issue in issues), key=stable_json)
    safe_duplicates = sorted((_issue(issue) for issue in duplicates), key=stable_json)
    safe_priority = [{key: row[key] for key in PRIORITY_COLUMNS if key in row and _scalar(row[key])} for row in priority]
    safe_priority.sort(key=lambda row: ({"REVIEW_REQUIRED": 0, "LIFECYCLE_REVIEW_REQUIRED": 1, "BLOCKED_FOR_IMPLEMENTATION": 2}.get(row.get("safety_gate"), 3), row.get("eligibility") != "ELIGIBLE", -row.get("total", 0), row.get("api_uid", "")))
    safe_summary = _summary(summary)
    safe_metadata = _metadata(metadata or {}, safe_records)
    names = ["api_inventory.json", "api_inventory.csv", "coverage_summary.json", "unresolved.csv", "duplicates.csv", "api_inventory_report.md", "priority_queue.csv"]
    _write_json(output / names[0], {"apis": safe_records, "issues": safe_issues, "duplicates": safe_duplicates, "metadata": safe_metadata})
    _write_csv(output / names[1], INVENTORY_COLUMNS, [_inventory_row(record) for record in safe_records])
    _write_json(output / names[2], safe_summary)
    unresolved_rows = [{"record_type": "UNRESOLVED_API", "api_uid": record["api_uid"], "display_identity": record.get("display_identity", ""), "category": ",".join(record.get("eligibility_reasons", [])), "details": record["issues"]} for record in safe_records if record.get("eligibility") == "UNRESOLVED"]
    unresolved_rows += [{"record_type": "ANALYZER_NOTICE", "category": issue.get("category", issue.get("code", "")), **issue, "details": issue} for issue in safe_issues]
    _write_csv(output / names[3], ("record_type", "api_uid", "display_identity", "category", "origin", "file", "line", "endpoint_id", "case_id", "details"), sorted(unresolved_rows, key=stable_json))
    _write_csv(output / names[4], ("category", "code", "api_uid", "count", "files", "endpoint_ids", "candidate_api_uids"), safe_duplicates)
    with _atomic_file(output / names[5]) as stream:
        stream.write(_markdown(safe_records, safe_summary, safe_issues, safe_duplicates, safe_priority, safe_metadata))
    _write_csv(output / names[6], PRIORITY_COLUMNS, safe_priority)
    return [str(output / name) for name in names]
