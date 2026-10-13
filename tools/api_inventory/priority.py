"""Transparent case-design recommendations, independent of execution permission."""

from .classifier import get_override, is_legacy


READONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def build_priority(records, overrides=None):
    """Rank uncovered APIs, retaining safety gates and independently visible scores."""
    queue = []
    seen = set()
    for record in sorted(records, key=lambda item: item["api_uid"]):
        if record["api_uid"] in seen:
            continue
        seen.add(record["api_uid"])
        if record.get("eligibility") == "EXCLUDED" or record.get("enabled_case_ids"):
            continue
        override = get_override(record, overrides) if overrides is not None else get_override(record, {record["api_uid"]: record.get("applied_override", {})})
        method = record.get("identity", {}).get("method", "ANY")
        destructive = any(item.get("destructive") is True for name in ("registry_records", "case_records") for item in record.get(name, []))
        destructive = destructive or any(item.get("mutation_candidate") is True for item in record.get("source_records", []))
        readonly = method in READONLY_METHODS and not destructive and not is_legacy(record)
        if "readonly" in override:
            readonly = override["readonly"] and not destructive
        safe_lifecycle = any(item.get("safe_lifecycle") is True for item in record.get("case_records", []))
        if not readonly and not safe_lifecycle:
            safety_gate = "BLOCKED_FOR_IMPLEMENTATION"
        elif not readonly:
            safety_gate = "LIFECYCLE_REVIEW_REQUIRED"
        else:
            safety_gate = "REVIEW_REQUIRED"
        registered = bool(record.get("endpoint_ids"))
        coverage_gap = 35 if registered else 50
        paired = bool(record.get("source_records") and record.get("postman_records"))
        evidence_confidence = {"HIGH": 20, "MEDIUM": 12, "LOW": 3}.get(record.get("confidence"), 0)
        if paired and record.get("confidence") == "HIGH":
            evidence_confidence = 25
        business_risk = override.get("business_risk", 0)
        module_weight = override.get("module_weight", 100)
        change_risk = 10 if readonly else (3 if safe_lifecycle else 0)
        automation_feasibility = 15 if readonly and paired and not is_legacy(record) else (8 if readonly else 2)
        if record.get("eligibility") != "ELIGIBLE":
            automation_feasibility = 0
        total = round(coverage_gap + evidence_confidence + business_risk * module_weight / 100 + change_risk + automation_feasibility, 6)
        queue.append({
            "api_uid": record["api_uid"], "display_identity": record.get("display_identity", ""),
            "method": method, "eligibility": record.get("eligibility", "UNRESOLVED"),
            "coverage_status": record.get("coverage_status", "UNRESOLVED"), "readonly": readonly,
            "coverage_gap": coverage_gap, "evidence_confidence": evidence_confidence,
            "business_risk": business_risk,
            "business_risk_source": "OVERRIDE" if "business_risk" in override else "UNCONFIGURED",
            "module_weight": module_weight, "change_risk": change_risk,
            "automation_feasibility": automation_feasibility, "total": total,
            "safety_gate": safety_gate,
        })
    gate_order = {"REVIEW_REQUIRED": 0, "LIFECYCLE_REVIEW_REQUIRED": 1, "BLOCKED_FOR_IMPLEMENTATION": 2}
    return sorted(queue, key=lambda row: (gate_order[row["safety_gate"]], row["eligibility"] != "ELIGIBLE", -row["total"], row["api_uid"]))
