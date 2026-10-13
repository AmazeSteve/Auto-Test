"""Offline orchestration; only local files are read and reports are written."""

import argparse
from pathlib import Path
import re
import sys

import yaml

from .case_adapter import scan_cases
from .classifier import classify
from .coverage import calculate_coverage
from .models import stable_json
from .postman_scanner import scan_postman
from .priority import build_priority
from .reconciler import reconcile
from .registry_adapter import scan_registry
from .reporter import write_reports
from .source_scanner import scan_source


OVERRIDE_ENUMS = {
    "eligibility": {"ELIGIBLE", "EXCLUDED", "UNRESOLVED"},
    "api_kind": {"HTTP_API", "PAGE_ROUTE", "UNKNOWN_ROUTE"},
    "resolution_status": {"RESOLVED"},
}
OVERRIDE_BOOLS = {"active", "deprecated", "internal", "readonly", "active_business_exposure"}
OVERRIDE_SCORES = {"business_risk", "module_weight"}


def load_overrides(path):
    if path is None:
        return {}
    with Path(path).open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    if not isinstance(document, dict) or set(document) != {"apis"} or not isinstance(document["apis"], dict):
        raise ValueError("Overrides must contain only an apis mapping")
    overrides = document["apis"]
    for uid, fields in overrides.items():
        if not isinstance(uid, str) or not re.fullmatch(r"[a-f0-9]{64}", uid) or not isinstance(fields, dict):
            raise ValueError("Overrides require full canonical UIDs and explicit fields")
        if not fields or set(fields) - (set(OVERRIDE_ENUMS) | OVERRIDE_BOOLS | OVERRIDE_SCORES):
            raise ValueError("Unknown or empty override fields")
        for key, value in fields.items():
            if key in OVERRIDE_ENUMS and value not in OVERRIDE_ENUMS[key]:
                raise ValueError("Invalid enum override")
            if key in OVERRIDE_BOOLS and not isinstance(value, bool):
                raise ValueError("Invalid boolean override")
            if key in OVERRIDE_SCORES and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not 0 <= value <= 100):
                raise ValueError("Invalid risk/weight override")
    return overrides


def _validate_output(repo, output, assets):
    output = output.resolve()
    for relative in ("data", "core", "services", "utils", ".git"):
        protected = (repo / relative).resolve()
        if output == protected or protected in output.parents:
            raise ValueError("Report output overlaps frozen production assets")
    for asset in assets:
        asset = Path(asset).resolve()
        if asset.is_dir() and (output == asset or asset in output.parents):
            raise ValueError("Report output overlaps input assets")
    if output.exists() and not output.is_dir():
        raise ValueError("Report output must be a directory")
    return output


def analyze(source, postman, repo_root, output_dir=None, overrides_path=None, service="wizbank"):
    if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", service):
        raise ValueError("Invalid analyzer service configuration")
    repo = Path(repo_root).resolve()
    if not (repo / "data/endpoints").is_dir() or not (repo / "data/cases").is_dir():
        raise ValueError("Repository endpoint/case directories are required")
    output = _validate_output(repo, Path(output_dir) if output_dir else repo / ".runtime/api_inventory",
                              (source, postman))
    overrides = load_overrides(overrides_path)
    source_scan = scan_source(source, service)
    postman_scan = scan_postman(postman, service)
    registry_scan = scan_registry(repo, service)
    case_scan = scan_cases(repo, registry_scan.records, service)
    scans = (source_scan, postman_scan, registry_scan, case_scan)
    records, issues, duplicates = reconcile(*scans)
    if set(overrides) - {record["api_uid"] for record in records}:
        raise ValueError("Override refers to absent canonical API")
    records = classify(records, overrides)
    summary = calculate_coverage(records)
    for scan in scans:
        summary["counts"].update(scan.counts)
    summary["counts"]["unresolved_evidence"] = len(issues)
    summary["duplicate_groups"] = len(duplicates)
    priority = build_priority(records, overrides)
    metadata = {"counts": summary["counts"], "overrides": overrides,
                "scanner_limitations": sorted({item for scan in scans for item in scan.limitations})}
    paths = write_reports(output, records, summary, issues, duplicates, priority, metadata)
    return {"records": records, "summary": summary, "issues": issues, "duplicates": duplicates,
            "priority": priority, "paths": paths}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline API inventory and static coverage analyzer")
    parser.add_argument("--source", required=True)
    parser.add_argument("--postman", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir")
    parser.add_argument("--overrides")
    parser.add_argument("--service", default="wizbank")
    args = parser.parse_args(argv)
    try:
        result = analyze(args.source, args.postman, args.repo_root, args.output_dir, args.overrides, args.service)
    except Exception as exc:
        # Parser exceptions may carry credentials from input; never echo them.
        print(f"Analyzer failed: {type(exc).__name__}; invalid input/configuration or report failure.", file=sys.stderr)
        return 2
    print(stable_json({"counts": result["summary"]["counts"], "report_count": len(result["paths"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
