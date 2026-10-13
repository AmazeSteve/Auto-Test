"""Authoritative canonical identity: complete deterministic JSON SHA-256."""

import hashlib
import re

from .models import stable_json
from .path_normalizer import normalize_path


CONDITION_KEYS = ("params", "headers", "consumes", "produces")
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "ANY"})
_SECRET_CONDITION_KEY = re.compile(r"authorization|cookie|password|passwd|credential|secret|token|"
                                   r"api[-_]?key|session[-_]?id", re.IGNORECASE)


def safe_condition(value):
    """Keep key/operator semantics, replace secret literals with stable digests.

    This accepts raw literals once at the identity boundary. A literal which
    happens to look like a digest is still hashed, keeping comparisons distinct.
    Presence-only conditions contain no comparison value.
    """
    value = value.strip()
    match = re.fullmatch(r"(.*?)(!=|=)(.*)", value, re.DOTALL)
    if match and _SECRET_CONDITION_KEY.search(match.group(1)):
        key, operator, literal = match.groups()
        literal = "sha256:" + hashlib.sha256(literal.encode("utf-8")).hexdigest()
        return key + operator + literal
    return value


def make_identity(service, method, path, mapping_conditions=None, dispatch_signature=None):
    if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", service):
        raise ValueError("Invalid canonical service")
    method = str(method).strip().upper()
    if method not in HTTP_METHODS:
        raise ValueError("Unresolved or unsupported HTTP method")
    conditions = mapping_conditions or {}
    if not isinstance(conditions, dict):
        raise ValueError("Mapping conditions must be a mapping")
    if set(conditions) - set(CONDITION_KEYS):
        raise ValueError("Unknown canonical mapping condition")
    normalized = {}
    for key in CONDITION_KEYS:
        values = conditions.get(key, [])
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set)) or not all(isinstance(value, str) for value in values):
            raise ValueError("Mapping conditions must contain literal strings")
        normalized[key] = sorted({safe_condition(value) for value in values if value.strip()})
    signature = dispatch_signature or []
    if isinstance(signature, dict):
        signature = [key + "=" + str(value) for key, value in signature.items()]
    if not isinstance(signature, (list, tuple, set)) or not all(isinstance(value, str) for value in signature):
        raise ValueError("Dispatch signature must contain literal strings")
    signature = [value.strip() for value in signature if value.strip()]
    return {"service": service, "method": method,
            "route_fingerprint": normalize_path(path, service)["route_fingerprint"],
            "mapping_conditions": normalized, "dispatch_signature": sorted(set(signature))}


def api_uid(identity):
    return hashlib.sha256(stable_json(identity).encode("utf-8")).hexdigest()


def display_identity(identity):
    pieces = [identity["service"], identity["method"], identity["route_fingerprint"]]
    for key in CONDITION_KEYS:
        if identity["mapping_conditions"][key]:
            pieces.append(key + ":" + ",".join(identity["mapping_conditions"][key]))
    pieces.extend(identity["dispatch_signature"])
    return " | ".join(pieces)


def identify(record, service="wizbank"):
    """Populate common safe evidence fields in place; callers keep only safe metadata."""
    record.update(normalize_path(record["runtime_path"], service))
    identity = make_identity(service, record["method"], record["canonical_path"],
                             record.get("mapping_conditions"), record.get("dispatch_signature"))
    record.update({"identity": identity, "api_uid": api_uid(identity), "display_identity": display_identity(identity)})
    record["mapping_conditions"] = identity["mapping_conditions"]
    return record
