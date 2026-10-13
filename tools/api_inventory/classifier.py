"""Conservative analyzer-only exposure and static coverage classification."""

import math


ELIGIBILITIES = frozenset({"ELIGIBLE", "EXCLUDED", "UNRESOLVED"})
API_KINDS = frozenset({"HTTP_API", "PAGE_ROUTE", "UNKNOWN_ROUTE"})
OVERRIDE_FIELDS = frozenset({
    "eligibility", "api_kind", "active", "deprecated", "internal",
    "resolution_status", "active_business_exposure", "business_risk",
    "module_weight", "readonly",
})
LEGACY_FAMILIES = {
    "/servlet/qdbAction": {"cmd"},
    "/servlet/Dispatcher": {"module", "cmd"},
    "/servlet/aeAction": {"cmd"},
}


def get_override(record, overrides=None):
    """Validate and project an explicit per-UID override; never retain extra fields."""
    override = (overrides or {}).get(record["api_uid"], {})
    if not isinstance(override, dict) or set(override) - OVERRIDE_FIELDS:
        raise ValueError("Unknown or malformed analyzer override")
    for key, value in override.items():
        if key == "eligibility" and value not in ELIGIBILITIES:
            raise ValueError("Invalid eligibility override")
        if key == "api_kind" and value not in API_KINDS:
            raise ValueError("Invalid API classification override")
        if key == "resolution_status" and value != "RESOLVED":
            raise ValueError("Identity resolution override must explicitly be RESOLVED")
        if key in {"active", "deprecated", "internal", "active_business_exposure", "readonly"} and not isinstance(value, bool):
            raise ValueError("Classification override must be a boolean")
        if key in {"business_risk", "module_weight"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError("Risk and weight overrides must be finite scores from 0 to 100")
    return dict(sorted(override.items()))


def is_legacy(record):
    identity = record.get("identity", {})
    return bool(identity.get("dispatch_signature")) or identity.get("route_fingerprint") in LEGACY_FAMILIES


def _issue_codes(record):
    return {
        issue.get("code", issue.get("category", "")) if isinstance(issue, dict) else str(issue)
        for issue in record.get("issues", [])
    }


def _unresolved_identity(record, override):
    if record.get("identity", {}).get("method") in {None, "", "ANY"}:
        return True
    if override.get("resolution_status") == "RESOLVED":
        return False
    if record.get("resolution_status", "RESOLVED") != "RESOLVED":
        return True
    for code in _issue_codes(record):
        if ("AMBIGUOUS" in code or "IDENTITY_COLLISION" in code
                or ("DUPLICATE" in code and "SOURCE" in code)
                or "UNRESOLVED" in code):
            return True
    return False


def _classify_one(record, override):
    sources = record.get("source_records", [])
    kinds = {source.get("api_kind", "UNKNOWN_ROUTE") for source in sources}
    kind = override.get("api_kind")
    if kind is None and len(kinds) == 1:
        kind = next(iter(kinds))
    deprecated = override.get("deprecated", bool(sources) and all(source.get("deprecated") is True for source in sources))
    internal = override.get("internal", bool(sources) and all(source.get("internal") is True for source in sources))
    active = override.get("active", bool(sources) and all(source.get("active") is True for source in sources))
    if override.get("eligibility") == "EXCLUDED":
        return "EXCLUDED", ["MANUAL_ELIGIBILITY_OVERRIDE"]
    if override.get("eligibility") == "UNRESOLVED":
        return "UNRESOLVED", ["MANUAL_ELIGIBILITY_OVERRIDE"]
    if deprecated:
        return "EXCLUDED", ["DEPRECATED"]
    if internal:
        return "EXCLUDED", ["INTERNAL"]
    if kind == "PAGE_ROUTE":
        return "EXCLUDED", ["PAGE_ROUTE"]
    source_inactive = bool(sources) and all(source.get("active") is False for source in sources)
    if ("active" in override and not active) or (source_inactive and kind == "HTTP_API"):
        return "EXCLUDED", ["INACTIVE"]
    if _unresolved_identity(record, override):
        return "UNRESOLVED", ["UNRESOLVED_IDENTITY"]
    if is_legacy(record):
        identity = record.get("identity", {})
        family = identity.get("route_fingerprint")
        keys = {part.split("=", 1)[0] for part in identity.get("dispatch_signature", []) if "=" in part and part.split("=", 1)[1]}
        if family not in LEGACY_FAMILIES or not LEGACY_FAMILIES[family] <= keys:
            return "UNRESOLVED", ["LEGACY_UNRESOLVED"]
        if not override.get("active_business_exposure"):
            return "UNRESOLVED", ["LEGACY_EXPOSURE_UNPROVEN"]
        return "ELIGIBLE", ["MANUAL_LEGACY_EXPOSURE_PROOF"]
    if override.get("eligibility") == "ELIGIBLE":
        return "ELIGIBLE", ["MANUAL_ELIGIBILITY_OVERRIDE"]
    source_resolved = override.get("resolution_status") == "RESOLVED" or all(source.get("resolution_status", "RESOLVED") == "RESOLVED" for source in sources)
    override_exposure = override.get("api_kind") == "HTTP_API" and override.get("active") is True
    if kind == "HTTP_API" and active and source_resolved and (sources or override_exposure):
        return "ELIGIBLE", ["RESOLVED_ACTIVE_HTTP_API"]
    return "UNRESOLVED", ["SOURCE_EXPOSURE_UNPROVEN" if not sources else "HTTP_CLASSIFICATION_UNPROVEN"]


def classify(records, overrides=None):
    """Mutate canonical records with tri-state eligibility and static case status."""
    for record in records:
        override = get_override(record, overrides)
        record["applied_override"] = override
        eligibility, reasons = _classify_one(record, override)
        record["eligibility"] = eligibility
        record["coverage_eligible"] = eligibility == "ELIGIBLE"
        record["eligibility_reasons"] = reasons
        if eligibility == "EXCLUDED":
            status = "EXCLUDED"
        elif record.get("enabled_case_ids") and not _unresolved_identity(record, override):
            status = "CASE_COVERED"
        elif is_legacy(record):
            status = "LEGACY_LOGICAL" if not _unresolved_identity(record, override) else "UNRESOLVED"
        elif record.get("endpoint_ids"):
            status = "REGISTERED_UNTESTED" if not _unresolved_identity(record, override) else "UNRESOLVED"
        elif record.get("source_records") and record.get("postman_records"):
            status = "SOURCE_POSTMAN_UNREGISTERED" if eligibility == "ELIGIBLE" else "UNRESOLVED"
        elif record.get("source_records"):
            status = "SOURCE_ONLY" if eligibility == "ELIGIBLE" else "UNRESOLVED"
        elif record.get("postman_records"):
            status = "POSTMAN_ONLY" if not _unresolved_identity(record, override) else "UNRESOLVED"
        else:
            status = "UNRESOLVED"
        record["coverage_status"] = status
    return records
