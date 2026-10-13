"""JSON-safe evidence containers shared by the independent analyzer modules."""

from dataclasses import dataclass, field


@dataclass
class ScanResult:
    records: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)


def stable_json(value):
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
