"""Frozen analyzer-only WizBank deployment and placeholder policy."""

import re
from urllib.parse import urlsplit


CONTEXT_PREFIXES = ("/app",)
_PLACEHOLDER = re.compile(r"\{\{[^{}]+\}\}|\$\{[^{}]+\}|\{[^{}]*\}")
_HOST_VARIABLE = re.compile(
    r"^\{\{(?:Host[^{}]*|local[_-]?host[^{}]*|base[_-]?url|url)\}\}", re.IGNORECASE)


def normalize_path(value, service="wizbank"):
    if not isinstance(value, str):
        raise ValueError("API path must be a string")
    value = value.strip()
    authority = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", value)
    if authority and authority.group(1).lower() not in {"http", "https"}:
        raise ValueError("Unsupported URL authority scheme")
    host_form = "RELATIVE"
    if value.lower().startswith(("http://", "https://")):
        parsed = urlsplit(value)
        value = parsed.path
        host_form = "ABSOLUTE_URL"
    elif value.startswith("//") and not value.startswith("///"):
        value = urlsplit(value).path
        host_form = "PROTOCOL_RELATIVE_URL"
    else:
        match = _HOST_VARIABLE.match(value)
        if match:
            value = value[match.end():]
            host_form = "HOST_VARIABLE"
    value = value.split("?", 1)[0].split("#", 1)[0]
    runtime = re.sub(r"/+", "/", "/" + value.lstrip("/")).rstrip("/") or "/"
    canonical = runtime
    if service == "wizbank":
        for prefix in CONTEXT_PREFIXES:
            if canonical == prefix:
                canonical = "/"
                break
            if canonical.startswith(prefix + "/"):
                canonical = canonical[len(prefix):]
                break
    return {"runtime_path": runtime, "canonical_path": canonical,
            "route_fingerprint": _PLACEHOLDER.sub("{}", canonical), "host_form": host_form}


def template_matches(template_fingerprint, concrete_fingerprint):
    template = template_fingerprint.strip("/").split("/")
    concrete = concrete_fingerprint.strip("/").split("/")
    return ("{}" in template and len(template) == len(concrete)
            and all(expected == "{}" or expected == actual for expected, actual in zip(template, concrete)))
