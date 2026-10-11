"""v2.4.6B1: deterministic, offline projection from a user Read Model.

No request or lifecycle integration is performed here. Missing dependencies are
errors, even when the corresponding Write Model field accepts an explicit null.
"""

from collections.abc import Mapping, Set
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


WRITE_MODEL_FIELDS = (
    "usr_ent_id", "usr_ste_usr_id", "usr_display_bil", "usr_nickname",
    "usr_gender", "usr_bday", "usr_email", "usr_tel_1", "usr_fax_1",
    "usr_extra_2", "urx_extra_41", "urx_extra_42", "usr_job_title",
    "usr_join_datetime", "eip_code", "urx_extra_44", "urx_extra_45",
    "userGroupId", "gradeId", "positionId", "tagId", "roleCode",
    "directLeaderId", "highestApprovalUserGroupId", "subordinateDepartmentId",
    "ssoConnectionEnable", "curLan",
)
SUPPORTED_MUTABLE_FIELDS = frozenset({"usr_display_bil"})
_DIRECT_FIELDS = tuple(
    field for field in WRITE_MODEL_FIELDS
    if field not in {"tagId", "directLeaderId", "curLan"}
)
_READ_DEPENDENCIES = _DIRECT_FIELDS + ("userTagVOList", "leaderList")


@dataclass(frozen=True)
class UserUpdatePayloadPlan:
    baseline_payload: dict[str, Any]
    mutation_payload: dict[str, Any]
    changed_fields: frozenset[str]


def _require_text(value: Any, field: str, *, max_length: int | None = None) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{field} must have length <= {max_length}")


def _require_id(value: Any, field: str) -> None:
    # Relation IDs may be encoded strings; raw numeric IDs must be positive.
    if isinstance(value, str) and value.strip():
        return
    if type(value) is int and value > 0:
        return
    raise ValueError(f"{field} must be a nonempty ID")


def _require_user_id(value: Any) -> None:
    # The genuine redacted form sample has the positive integer ID 1830.
    if type(value) is int and value > 0:
        return
    # Positive ASCII decimal strings are a compatibility policy, not the
    # observed JSON type; preserve their representation rather than coercing it.
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        if int(value) > 0:
            return
    raise ValueError("usr_ent_id must be a positive integer or decimal string")


def _require_role_codes(value: Any) -> None:
    _require_text(value, "roleCode")
    seen = set()
    for token in value.split(","):
        _require_text(token, "roleCode token")
        identity = token.strip()
        if identity in seen:
            raise ValueError("roleCode contains duplicate tokens")
        seen.add(identity)


def _join_relation_ids(
    values: Any, *, field: str, id_field: str, nullable: bool = False
) -> str | None:
    if values is None and nullable:
        return None
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list" + (" or null" if nullable else ""))
    identifiers = []
    seen = set()
    for entry in values:
        if not isinstance(entry, Mapping) or id_field not in entry:
            raise ValueError(f"{field} entries must contain {id_field}")
        identifier = entry[id_field]
        _require_text(identifier, f"{field}.{id_field}")
        if identifier in seen:
            raise ValueError(f"{field} contains duplicate {id_field}")
        seen.add(identifier)
        identifiers.append(identifier)
    return ",".join(identifiers) if identifiers else None


class UserUpdatePayloadBuilder:
    @staticmethod
    def build(
        read_model: Mapping[str, Any],
        *,
        cur_lan: str,
        mutable_fields: Set[str],
        overrides: Mapping[str, Any],
    ) -> UserUpdatePayloadPlan:
        if not isinstance(read_model, Mapping):
            raise ValueError("read_model must be a mapping")
        if not isinstance(mutable_fields, Set):
            raise ValueError("mutable_fields must be a set")
        requested = frozenset(mutable_fields)
        if not requested <= SUPPORTED_MUTABLE_FIELDS:
            raise ValueError("mutable_fields contains unsupported fields")
        if not isinstance(overrides, Mapping) or set(overrides) != requested:
            raise ValueError("overrides keys must equal mutable_fields")
        _require_text(cur_lan, "curLan")

        missing = [field for field in _READ_DEPENDENCIES if field not in read_model]
        if missing:
            raise ValueError("read_model missing required keys: " + ", ".join(missing))

        _require_user_id(read_model["usr_ent_id"])
        _require_text(read_model["usr_ste_usr_id"], "usr_ste_usr_id")
        _require_text(read_model["usr_display_bil"], "usr_display_bil", max_length=80)
        if read_model["usr_gender"] not in ("M", "F", ""):
            raise ValueError("usr_gender must be M, F or an empty string")
        for field in ("userGroupId", "gradeId"):
            _require_id(read_model[field], field)
        for field in ("positionId", "highestApprovalUserGroupId", "subordinateDepartmentId"):
            if read_model[field] is not None:
                _require_id(read_model[field], field)
        _require_role_codes(read_model["roleCode"])
        if read_model["ssoConnectionEnable"] not in ("Yes", "No"):
            raise ValueError("ssoConnectionEnable must be Yes or No")

        projected = {field: read_model[field] for field in _DIRECT_FIELDS}
        projected["tagId"] = _join_relation_ids(
            read_model["userTagVOList"], field="userTagVOList", id_field="encryId"
        )
        projected["directLeaderId"] = _join_relation_ids(
            read_model["leaderList"], field="leaderList",
            id_field="usr_ent_id_encrypt", nullable=True,
        )
        projected["curLan"] = cur_lan
        baseline = deepcopy({field: projected[field] for field in WRITE_MODEL_FIELDS})
        mutation = deepcopy(baseline)
        for field in requested:
            _require_text(overrides[field], field, max_length=80)
            mutation[field] = deepcopy(overrides[field])

        actual_changed_fields = frozenset(
            field for field in WRITE_MODEL_FIELDS if baseline[field] != mutation[field]
        )
        if actual_changed_fields != requested:
            raise ValueError("actual_changed_fields must equal mutable_fields")
        return UserUpdatePayloadPlan(baseline, mutation, actual_changed_fields)
