"""Unique-canonical-API metrics; static coverage does not assert execution."""

from collections import Counter, defaultdict

from .models import stable_json


def _metric(numerator, denominator):
    value = numerator / denominator if denominator else None
    return {"numerator": numerator, "denominator": denominator, "value": value,
            "percentage": value * 100 if value is not None else None,
            "status": "APPLICABLE" if denominator else "NOT_APPLICABLE"}


def calculate_coverage(records):
    """Compute three documented metrics over UID sets, never case-record counts."""
    grouped = defaultdict(list)
    for record in records:
        grouped[record["api_uid"]].append(record)
    eligible, registered, covered = set(), set(), set()
    eligibility_counts = Counter()
    status_counts = Counter()
    evidence = {name: set() for name in ("source_records", "postman_records", "registry_records")}
    enabled, disabled = set(), set()
    for uid, group in grouped.items():
        states = {record.get("eligibility", "UNRESOLVED") for record in group}
        state = next(iter(states)) if len(states) == 1 else "UNRESOLVED"
        eligibility_counts[state] += 1
        statuses = {record.get("coverage_status", "UNRESOLVED") for record in group}
        status_counts[next(iter(statuses)) if len(statuses) == 1 else "UNRESOLVED"] += 1
        if state == "ELIGIBLE":
            eligible.add(uid)
        if any(record.get("endpoint_ids") for record in group):
            registered.add(uid)
        if any(record.get("enabled_case_ids") for record in group):
            covered.add(uid)
        for record in group:
            for name in evidence:
                evidence[name].update(stable_json(item) for item in record.get(name, []))
            enabled.update(record.get("enabled_case_ids", []))
            disabled.update(record.get("disabled_case_ids", []))
    return {
        "inventory_registration_coverage": _metric(len(eligible & registered), len(eligible)),
        "static_case_coverage": _metric(len(eligible & covered), len(eligible)),
        "registry_utilization": _metric(len(registered & covered), len(registered)),
        "counts": {
            "source_candidates": len(evidence["source_records"]), "canonical_apis": len(grouped),
            "eligible": eligibility_counts["ELIGIBLE"], "excluded": eligibility_counts["EXCLUDED"],
            "unresolved": eligibility_counts["UNRESOLVED"],
            "postman_requests": len(evidence["postman_records"]), "postman_empty_urls": 0,
            "registry_definitions": len(evidence["registry_records"]),
            "enabled_cases": len(enabled), "disabled_cases": len(disabled),
        },
        "coverage_status_counts": dict(sorted(status_counts.items())),
    }
