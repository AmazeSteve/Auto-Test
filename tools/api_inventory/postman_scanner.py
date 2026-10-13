"""Read Postman request evidence offline and project safe metadata only.

No raw request, auth, host, query value or body value leaves this module. The
only parameter values allowed into identity are validated legacy dispatch
literals. Environment/global documents are never used for variable resolution.
"""

import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlsplit

import yaml

from .identity import HTTP_METHODS, identify
from .inputs import iter_files
from .legacy import legacy_signature
from .models import ScanResult
from .path_normalizer import normalize_path


_BODY_TYPES = frozenset({"json", "text", "raw", "urlencoded", "formdata",
                         "form-data", "file", "binary", "graphql", "none"})
_LIMITATIONS = [
    "Postman variables, scripts, authentication, environments and globals are not evaluated.",
    "Opaque text, binary and file bodies provide no body field keys.",
    "Legacy dispatch literal validation conservatively rejects credential-like names and ambiguous repeated values.",
    "Legacy identities do not establish active servlet deployment or business exposure.",
]


def _pairs(value):
    """Read dict or Postman key/value-list fields; disabled entries do not apply."""
    if isinstance(value, dict):
        yield from ((key, val) for key, val in value.items() if isinstance(key, str))
    elif isinstance(value, list):
        for item in value:
            if (isinstance(item, dict) and isinstance(item.get("key"), str)
                    and not item.get("disabled", False)):
                yield item["key"], item.get("value")


def _keys(pairs):
    return sorted({key.strip() for key, _ in pairs if key.strip()})


def _query_pairs(raw_pairs, structured):
    """Explicit structured fields override their raw representation, including disabled fields."""
    if isinstance(structured, dict):
        represented = {key for key in structured if isinstance(key, str)}
    elif isinstance(structured, list):
        represented = {item["key"] for item in structured
                       if isinstance(item, dict) and isinstance(item.get("key"), str)}
    else:
        represented = set()
    return [(key, value) for key, value in raw_pairs if key not in represented] + list(_pairs(structured))


def _json_keys(value):
    """Field names are safe; scalar values are deliberately never converted."""
    keys = set()
    pending = [(value, frozenset())]
    while pending:
        value, ancestors = pending.pop()
        if not isinstance(value, (dict, list)):
            continue
        if id(value) in ancestors:
            raise ValueError("Cyclic Postman body structure")
        ancestors = ancestors | {id(value)}
        if isinstance(value, dict):
            keys.update(key for key in value if isinstance(key, str))
            children = value.values()
        else:
            children = value
        pending.extend((child, ancestors) for child in children)
    return keys


def _body_metadata(body):
    """Return type, safe field keys and ephemeral servlet form parameters."""
    if body is None:
        return "none", [], []
    if not isinstance(body, dict):
        return "unknown", [], []
    if body.get("disabled", False):
        return "none", [], []
    declared = body.get("type", body.get("mode", "none"))
    body_type = declared.lower() if isinstance(declared, str) else "unknown"
    if body_type not in _BODY_TYPES:
        body_type = "unknown"
    content = body.get("content")
    if content is None:
        content = body.get(body_type)
    if body_type == "raw":
        content = body.get("raw", content)
        options = body.get("options")
        if (isinstance(options, dict) and isinstance(options.get("raw"), dict)
                and options["raw"].get("language") == "json"):
            body_type = "json"
    if body_type == "json":
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (ValueError, RecursionError):
                return body_type, [], []
        return body_type, sorted(_json_keys(content)), []
    if body_type in {"formdata", "form-data", "urlencoded"}:
        if isinstance(content, str) and body_type == "urlencoded":
            pairs = parse_qsl(content, keep_blank_values=True)
        else:
            pairs = list(_pairs(content))
        form_pairs = pairs
        if isinstance(content, list) and body_type in {"formdata", "form-data"}:
            form_pairs = list(_pairs([item for item in content if isinstance(item, dict)
                                      and item.get("type") != "file" and "src" not in item]))
        return body_type, _keys(pairs), form_pairs
    if body_type == "graphql" and isinstance(content, dict):
        variables = content.get("variables")
        if isinstance(variables, str):
            try:
                variables = json.loads(variables)
            except (ValueError, RecursionError):
                variables = None
        return body_type, sorted(_json_keys(variables)), []
    return body_type, [], []


def _url_metadata(url, service):
    """Normalize URL string/object without retaining the host or query values."""
    structured_query = None
    host_form = None
    if isinstance(url, dict):
        structured_query = url.get("query")
        raw = url.get("raw")
        if isinstance(raw, str) and raw.strip():
            url = raw
        else:
            path = url.get("path")
            if isinstance(path, list) and all(isinstance(part, str) for part in path):
                path = "/".join(path)
            if not isinstance(path, str) or not path.strip():
                return None, list(_pairs(structured_query))
            host = url.get("host")
            if isinstance(host, list):
                host = ".".join(part for part in host if isinstance(part, str))
            host_form = ("HOST_VARIABLE" if isinstance(host, str) and "{{" in host
                         else "ABSOLUTE_URL" if host else "RELATIVE")
            url = "/" + path.lstrip("/")
    if not isinstance(url, str) or not url.strip():
        return None, list(_pairs(structured_query))
    url = url.strip()
    parsed = urlsplit(url)
    normalized_url = parsed.path or "/"
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Unsupported Postman URL")
        normalized_url = parsed.path or "/"
        host_form = "ABSOLUTE_URL"
    elif url.startswith("//") and not url.startswith("///"):
        normalized_url = parsed.path or "/"
        host_form = "PROTOCOL_RELATIVE_URL"
    elif "://" in parsed.path:
        raise ValueError("Unsupported Postman URL")
    normalized = normalize_path(normalized_url, service)
    if host_form is not None:
        normalized["host_form"] = host_form
    # urlsplit is used only to read ephemeral query parameters. It is not output.
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    return normalized, _query_pairs(pairs, structured_query)


def _requests(document):
    """Yield (request name, request) for actual YAML and standard collections."""
    if not isinstance(document, dict):
        return
    if document.get("$kind") == "http-request":
        yield document.get("name"), document
        return
    # A kind-bearing document is not a standard Collection JSON document.
    if "$kind" in document:
        return
    info = document.get("info")
    items = document.get("item")
    if not isinstance(info, dict) or not isinstance(items, list):
        return

    pending = [(item, frozenset({id(items)})) for item in reversed(items)]
    while pending:
        item, ancestors = pending.pop()
        if not isinstance(item, dict):
            continue
        request = item.get("request")
        if isinstance(request, dict):
            yield item.get("name"), request
        elif isinstance(request, str):
            # The collection schema permits a URL shorthand but supplies no
            # HTTP method. Preserve an unresolved issue rather than guess GET.
            yield item.get("name"), {"url": request}
        children = item.get("item")
        if isinstance(children, list):
            if id(children) in ancestors:
                raise ValueError("Cyclic Postman collection structure")
            ancestors = ancestors | {id(children)}
            pending.extend((child, ancestors) for child in reversed(children))


def _packaging_copy(left, right):
    """Require an archive boundary and an identical full member-path suffix."""
    if left.count("!") == right.count("!"):
        return False
    left_member = left.rsplit("!", 1)[-1]
    right_member = right.rsplit("!", 1)[-1]
    return (left_member == right_member
            or left_member.endswith("/" + right_member)
            or right_member.endswith("/" + left_member))


def _issue(result, category, file, request_name=None):
    issue = {"origin": "postman", "category": category, "file": file}
    if request_name is not None:
        issue["request_name"] = request_name
    result.issues.append(issue)


def scan_postman(asset, service="wizbank"):
    """Scan local inputs, including nested ZIPs, with deterministic evidence."""
    result = ScanResult(limitations=list(_LIMITATIONS))
    result.counts = {"postman_files": 0, "raw_postman_requests": 0,
                     "postman_requests": 0, "postman_empty_urls": 0,
                     "packaging_duplicates": 0}
    parsed = []
    for file, data in iter_files(asset, {".yaml", ".yml", ".json"}, nested_archives=True):
        result.counts["postman_files"] += 1
        lower = file.lower()
        if lower.endswith((".postman_environment.json", ".postman_globals.json")):
            continue
        try:
            document = json.loads(data) if lower.endswith(".json") else yaml.safe_load(data)
        except (ValueError, UnicodeError, yaml.YAMLError, RecursionError):
            # Parser exceptions may quote values; never propagate their messages.
            raise ValueError("Invalid Postman analysis document: " + file) from None
        requests = list(_requests(document))
        if not requests:
            continue
        result.counts["raw_postman_requests"] += len(requests)
        parsed.append((file, hashlib.sha256(data).digest(), requests))
    accepted = []
    for file, digest, requests in sorted(parsed, key=lambda item: (item[0].count("!"), item[0])):
        matches = [other for other, other_digest in accepted
                   if digest == other_digest and _packaging_copy(file, other)]
        if len(matches) == 1:
            result.counts["packaging_duplicates"] += len(requests)
            continue
        accepted.append((file, digest))
        for name, request in requests:
            result.counts["postman_requests"] += 1
            # Actual YAML commonly omits name: derive it from the relative filename.
            name = name if isinstance(name, str) else PurePosixPath(file).name.removesuffix(".request.yaml")
            try:
                normalized, url_pairs = _url_metadata(request.get("url"), service)
            except ValueError:
                _issue(result, "POSTMAN_INVALID_URL", file, name)
                continue
            if normalized is None:
                result.counts["postman_empty_urls"] += 1
                _issue(result, "POSTMAN_EMPTY_URL", file, name)
                continue
            method = request.get("method")
            method = method.strip().upper() if isinstance(method, str) else ""
            if method not in HTTP_METHODS or method == "ANY":
                _issue(result, "POSTMAN_UNRESOLVED_METHOD", file, name)
                continue
            query_pairs = _query_pairs(url_pairs, request.get("queryParams"))
            headers = list(_pairs(request.get("headers", request.get("header"))))
            body_type, body_keys, form_pairs = _body_metadata(request.get("body"))
            parameters = {}
            for key, value in query_pairs + form_pairs:
                parameters.setdefault(key, []).append(value)
            signature, status = legacy_signature(normalized["runtime_path"], parameters)
            record = {"origin": "postman", "file": file, "request_name": name,
                      "method": method, "runtime_path": normalized["runtime_path"],
                      "query_keys": _keys(query_pairs), "header_keys": _keys(headers),
                      "body_type": body_type, "body_keys": body_keys,
                      "host_form": normalized["host_form"],
                      "resolution_status": status, "dispatch_signature": signature}
            if status == "RESOLVED":
                identify(record, service)
                # identify receives a path, so preserve original host classification.
                record["host_form"] = normalized["host_form"]
            else:
                record.update({key: normalized[key] for key in ("canonical_path", "route_fingerprint")})
                _issue(result, status, file, name)
            result.records.append(record)
    result.records.sort(key=lambda record: (record["file"], record["request_name"], record["method"], record["runtime_path"]))
    result.issues.sort(key=lambda issue: (issue["file"], issue.get("request_name", ""), issue["category"]))
    return result
