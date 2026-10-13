"""Pure C1 comparison of the frozen normalized User Write Model."""

from copy import deepcopy

from wizbank_api_test.services.user_update_payload_builder import (
    WRITE_MODEL_FIELDS,
    UserUpdatePayloadBuilder,
)


RELATION_FIELDS = frozenset({"roleCode", "tagId", "directLeaderId"})


def normalize(read_model, cur_lan):
    return UserUpdatePayloadBuilder.build(
        read_model, cur_lan=cur_lan, mutable_fields=set(), overrides={},
    ).baseline_payload


def canonicalize(payload):
    if not isinstance(payload, dict) or set(payload) != set(WRITE_MODEL_FIELDS):
        raise ValueError("C1 state requires exactly the frozen 27 Write Model fields")
    state = deepcopy(payload)
    for field in RELATION_FIELDS:
        value = state[field]
        if value is None:
            state[field] = ()
        elif isinstance(value, str):
            state[field] = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
        else:
            raise ValueError(f"C1 relation {field} must be comma text or null")
    return state


def project(read_model, cur_lan):
    return canonicalize(normalize(read_model, cur_lan))


def diff(before, after):
    """Raw differences stay in memory; evidence must use keys/hashes only."""
    return {
        field: {"before": deepcopy(before[field]), "after": deepcopy(after[field])}
        for field in WRITE_MODEL_FIELDS
        if type(before[field]) is not type(after[field]) or before[field] != after[field]
    }


def assert_mutation_only(before, after, allowed=frozenset({"usr_display_bil"})):
    changes = diff(before, after)
    if set(changes) != set(allowed):
        raise AssertionError(f"C1 mutation changed unexpected fields: {sorted(changes)}")
    return changes


def assert_restored(before, restored):
    changes = diff(before, restored)
    if changes:
        raise AssertionError(f"C1 restore drift in fields: {sorted(changes)}")
    return changes
