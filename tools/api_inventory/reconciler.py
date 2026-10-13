"""Canonical reconciliation without fuzzy names or inferred dynamic segments."""

from collections import defaultdict
from copy import deepcopy

from .models import stable_json
from .path_normalizer import template_matches


EVIDENCE_FIELDS = {
    "source": "source_records", "postman": "postman_records",
    "registry": "registry_records", "case": "case_records",
}


def _same_constraints(left, right):
    return all(left[key] == right[key] for key in
               ("service", "method", "mapping_conditions", "dispatch_signature"))


def reconcile(*scans):
    """Return (canonical records, safe issues, duplicate groups).

    Exact identities win. Only known source templates can absorb concrete routes,
    and conditions must agree. A path-only match never erases Spring conditions.
    Inputs are not mutated; original concrete evidence remains in each API.
    """
    evidence = [deepcopy(record) for scan in scans for record in scan.records]
    issues = [deepcopy(issue) for scan in scans for issue in scan.issues]
    for issue in issues:
        issue.setdefault("category", issue.get("code", "UNRESOLVED_EVIDENCE"))
    sources = {record["api_uid"]: record for record in evidence
               if record.get("origin") == "source" and record.get("api_uid")
               and record.get("resolution_status", "RESOLVED") == "RESOLVED"}
    exact_uids = {record["api_uid"] for record in evidence
                  if record.get("origin") == "source" and record.get("api_uid")}
    groups = defaultdict(list)
    for record in sorted(evidence, key=stable_json):
        uid = record.get("api_uid")
        if not uid:
            # Scanner issues describe unresolved nonliteral paths; they are not
            # invented canonical APIs and cannot inflate the denominator.
            continue
        if record["origin"] != "source" and uid not in exact_uids:
            identity = record["identity"]
            candidates = sorted(candidate_uid for candidate_uid, source in sources.items()
                                if _same_constraints(source["identity"], identity)
                                and template_matches(source["identity"]["route_fingerprint"],
                                                     identity["route_fingerprint"]))
            if len(candidates) == 1:
                record["reconciliation"] = "UNIQUE_SOURCE_TEMPLATE"
                record["original_api_uid"] = uid
                uid = candidates[0]
            elif len(candidates) > 1:
                record["resolution_status"] = "AMBIGUOUS_DYNAMIC_MATCH"
                issues.append({"category": "AMBIGUOUS_DYNAMIC_MATCH", "file": record["file"],
                               "api_uid": uid, "candidate_api_uids": candidates})
        groups[uid].append(record)

    inventory, duplicates = [], []
    for uid, records in sorted(groups.items()):
        primary = sources.get(uid) or next(record for record in records if record["api_uid"] == uid)
        api = {"api_uid": uid, "identity": deepcopy(primary["identity"]),
               "display_identity": primary["display_identity"],
               "runtime_paths": sorted({record["runtime_path"] for record in records}),
               "canonical_paths": sorted({record["canonical_path"] for record in records}),
               "issues": [], "resolution_status": "RESOLVED"}
        for origin, field in EVIDENCE_FIELDS.items():
            api[field] = sorted((record for record in records if record["origin"] == origin), key=stable_json)
        source_records = api["source_records"]
        # Multiple declarations of the same HTTP identity are an unresolved
        # collision, even if mapping evidence otherwise looks valid.
        declarations = {(r.get("file"), r.get("line"), r.get("java_method")) for r in source_records}
        if len(declarations) > 1:
            api["issues"].append("SOURCE_IDENTITY_COLLISION")
            duplicates.append({"category": "SOURCE_IDENTITY_COLLISION", "api_uid": uid,
                               "count": len(declarations),
                               "files": sorted({r["file"] for r in source_records})})
        for field, category in (("registry_records", "DUPLICATE_REGISTRY"),
                                ("postman_records", "DUPLICATE_POSTMAN_EVIDENCE")):
            if len(api[field]) > 1:
                duplicates.append({"category": category, "api_uid": uid,
                                   "count": len(api[field]),
                                   "files": sorted({r["file"] for r in api[field]})})
        unresolved = sorted({record.get("resolution_status", "RESOLVED") for record in records}
                            - {"RESOLVED"})
        api["issues"].extend(unresolved)
        api["issues"] = sorted(set(api["issues"]))
        identity_issues = [code for code in api["issues"]
                           if "AMBIGUOUS" in code or "UNRESOLVED" in code or "IDENTITY_COLLISION" in code]
        if identity_issues:
            api["resolution_status"] = "UNRESOLVED"
        api["endpoint_ids"] = sorted({r["endpoint_id"] for r in api["registry_records"]
                                      if r.get("endpoint_id") and r.get("resolution_status") == "RESOLVED"})
        api["registry_files"] = sorted({r["file"] for r in api["registry_records"]})
        api["enabled_case_ids"] = sorted({r["case_id"] for r in api["case_records"] if r["enabled"]
                                          and r.get("case_id") and r.get("resolution_status") == "RESOLVED"})
        api["disabled_case_ids"] = sorted({r["case_id"] for r in api["case_records"] if not r["enabled"]
                                           and r.get("case_id")})
        api["confidence"] = ("UNRESOLVED" if identity_issues else
                             "HIGH" if source_records and api["postman_records"] else
                             "MEDIUM" if source_records else "LOW")
        inventory.append(api)
    return inventory, sorted(issues, key=stable_json), sorted(duplicates, key=stable_json)
