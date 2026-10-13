"""Conservative logical identity for the three proven servlet transports.

These rules describe identity, never servlet deployment/exposure. The analyzer
did not establish servlet deployment exposure from the supported scanned evidence, so consumers
must leave legacy eligibility UNRESOLVED unless an explicit override supplies it.
aeReqParam.java:429-432 and aeAction.java:300,316 establish ``cmd`` for aeAction;
they do not establish ``action`` as a dispatch parameter.
"""

import re
from collections.abc import Mapping

from .path_normalizer import normalize_path


LEGACY_DISPATCH_FIELDS = {
    "/servlet/qdbAction": ("cmd",),
    "/servlet/Dispatcher": ("module", "cmd"),
    "/servlet/aeAction": ("cmd",),
}

_LOGICAL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}\Z")
_CREDENTIAL_WORD = re.compile(
    r"bearer|authorization|cookie|password|passwd|pwd|credential|secret|token|apikey|sessionid|privatekey|csrf|xsrf",
    re.IGNORECASE,
)
_OPAQUE_HEX = re.compile(r"[0-9a-fA-F]{24,}\Z")
_JWT = re.compile(r"[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\Z")


def is_legacy(path):
    """Recognize only exact known transports after the context-prefix policy."""
    return normalize_path(path)["canonical_path"] in LEGACY_DISPATCH_FIELDS


def _logical_literal(value):
    if not isinstance(value, str):
        return None
    if value != value.strip():
        return None
    if (not _LOGICAL_IDENTIFIER.fullmatch(value)
            or _CREDENTIAL_WORD.search(re.sub(r"[_:.-]", "", value))
            or _OPAQUE_HEX.fullmatch(value)
            or _JWT.fullmatch(value)):
        return None
    return value


def legacy_signature(path, parameter_values):
    """Return safe dispatch literals and RESOLVED/LEGACY_UNRESOLVED.

    ``parameter_values`` maps parameter names to either a value or a sequence of
    values. Conflicting repeats, missing fields and nonliteral values remain
    unresolved. Ordinary parameters are never inspected or retained. Normal
    routes return an empty signature and RESOLVED.
    """
    required = LEGACY_DISPATCH_FIELDS.get(normalize_path(path)["canonical_path"])
    if required is None:
        return [], "RESOLVED"
    if not isinstance(parameter_values, Mapping):
        return [], "LEGACY_UNRESOLVED"
    signature = []
    for key in required:
        values = parameter_values.get(key)
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        literals = [_logical_literal(value) for value in values]
        if not literals or None in literals or len(set(literals)) != 1:
            return [], "LEGACY_UNRESOLVED"
        signature.append(key + "=" + literals[0])
    return sorted(signature), "RESOLVED"
