"""Read endpoint YAML as safe offline evidence, independent of runtime state."""

from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

from .identity import HTTP_METHODS, identify
from .legacy import is_legacy, legacy_signature
from .models import ScanResult
from .path_normalizer import normalize_path


def _read_yaml_records(repo_root, directory):
    """Yield private YAML dictionaries; never put their contents in a report."""
    root = Path(repo_root).resolve()
    base = root / directory
    if not base.is_dir():
        raise ValueError(f"Missing framework asset directory: {directory}")
    for path in sorted(base.rglob("*.yaml")):
        relative = path.relative_to(root).as_posix()
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Framework YAML symlink escapes repository: {relative}")
        try:
            content = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            # Parser exceptions can quote body/header/query secrets.
            raise ValueError(f"Cannot parse framework YAML: {relative}") from None
        if not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
            raise ValueError(f"Framework YAML must contain a list of mappings: {relative}")
        for index, item in enumerate(content, start=1):
            yield relative, index, item


def _host_class(value):
    if value is None or value == "":
        return "ABSENT"
    if not isinstance(value, str):
        return "OTHER"
    if value.startswith(("http://", "https://", "//")):
        return "ABSOLUTE_URL"
    if "{{" in value or "${" in value:
        return "VARIABLE"
    return "CONFIG_KEY"


def _auth_required(value):
    return value is not None and value is not False and value != "" and value != "none"


def _routing_parameters(definition):
    """Servlet parameters are private; JSON bodies do not prove getParameter."""
    parameters = defaultdict(list)
    path = definition.get("path")
    if isinstance(path, str):
        for key, values in parse_qs(urlsplit(path).query, keep_blank_values=True).items():
            parameters[key].extend(values)
    for field in ("params", "data"):
        values = definition.get(field)
        if isinstance(values, dict):
            for key, value in values.items():
                parameters[key].extend(value if isinstance(value, (list, tuple)) else [value])
    return parameters


def _identify_request(record, definition, service, issues):
    """Populate identity or an explicit unresolved status without raw values."""
    location = {key: record[key] for key in ("origin", "file", "endpoint_id", "case_id") if key in record}
    raw_method = definition.get("method")
    method = raw_method.strip().upper() if isinstance(raw_method, str) else "UNKNOWN"
    path = definition.get("path")
    record["method"] = method if method in HTTP_METHODS else "UNKNOWN"
    if not isinstance(path, str) or not path.strip():
        record.update(runtime_path="", resolution_status="UNRESOLVED_PATH")
        issues.append({"code": "UNRESOLVED_PATH", **location})
        return record
    try:
        record.update(normalize_path(path, service))
    except ValueError:
        record.update(runtime_path="", resolution_status="UNRESOLVED_PATH")
        issues.append({"code": "UNRESOLVED_PATH", **location})
        return record
    if method not in HTTP_METHODS:
        record["resolution_status"] = "UNRESOLVED_METHOD"
        issues.append({"code": "UNRESOLVED_METHOD", **location})
        return record
    if method == "ANY":
        record["resolution_status"] = "AMBIGUOUS_METHOD"
        issues.append({"code": "AMBIGUOUS_METHOD", **location})
        return record
    if is_legacy(record["canonical_path"]):
        record["legacy"] = True
        signature, status = legacy_signature(record["canonical_path"], _routing_parameters(definition))
        if status != "RESOLVED":
            record["resolution_status"] = status
            issues.append({"code": status, **location})
            return record
        record["dispatch_signature"] = signature
    conditions = definition.get("mapping_conditions")
    if conditions is not None:
        # Request headers are never interpreted as Spring mapping conditions.
        record["mapping_conditions"] = conditions
    try:
        identify(record, service)
    except (TypeError, ValueError):
        # Malformed identity input is reportable, but never echo its contents.
        record.pop("mapping_conditions", None)
        record.pop("dispatch_signature", None)
        record["resolution_status"] = "UNRESOLVED_IDENTITY"
        issues.append({"code": "UNRESOLVED_IDENTITY", **location})
        return record
    record["mapping_conditions"] = record["identity"]["mapping_conditions"]
    record["resolution_status"] = "RESOLVED"
    return record


def scan_registry(repo_root, service="wizbank"):
    """Collect definitions and canonical duplicates without a Registry import."""
    result = ScanResult(counts={"registry_definitions": 0})
    by_id = defaultdict(list)
    by_uid = defaultdict(list)
    for relative, index, definition in _read_yaml_records(repo_root, "data/endpoints"):
        result.counts["registry_definitions"] += 1
        endpoint_id = definition.get("id")
        if not isinstance(endpoint_id, str) or not endpoint_id.strip():
            endpoint_id = ""
            result.issues.append({"code": "MISSING_ENDPOINT_ID", "origin": "registry", "file": relative, "index": index})
        record = {
            "origin": "registry", "endpoint_id": endpoint_id, "file": relative,
            "index": index, "host_class": _host_class(definition.get("host")),
            "auth_required": _auth_required(definition.get("auth")),
            "destructive": definition.get("destructive") is True,
        }
        if isinstance(definition.get("module"), str):
            record["module"] = definition["module"]
        _identify_request(record, definition, service, result.issues)
        if not endpoint_id:
            record["resolution_status"] = "MISSING_ENDPOINT_ID"
        result.records.append(record)
        if endpoint_id:
            by_id[endpoint_id].append(record)
        if record.get("api_uid"):
            by_uid[record["api_uid"]].append(record)
    for endpoint_id, records in sorted(by_id.items()):
        if len(records) > 1:
            result.issues.append({"code": "DUPLICATE_ENDPOINT_ID", "origin": "registry", "endpoint_id": endpoint_id,
                                  "files": sorted({record["file"] for record in records}), "count": len(records)})
    for uid, records in sorted(by_uid.items()):
        if len(records) > 1:
            result.issues.append({"code": "DUPLICATE_REGISTRY_DEFINITION", "origin": "registry", "api_uid": uid,
                                  "endpoint_ids": sorted({record["endpoint_id"] for record in records}),
                                  "files": sorted({record["file"] for record in records}), "count": len(records)})
    return result
