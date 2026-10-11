"""Offline B1 checks; RFC B1-01..B1-45 mapping is recorded in the B1 document."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from wizbank_api_test.services.user_update_payload_builder import (
    SUPPORTED_MUTABLE_FIELDS,
    UserUpdatePayloadBuilder,
    UserUpdatePayloadPlan,
)


EXPECTED_FIELDS = {
    "usr_ent_id", "usr_ste_usr_id", "usr_display_bil", "usr_nickname",
    "usr_gender", "usr_bday", "usr_email", "usr_tel_1", "usr_fax_1",
    "usr_extra_2", "urx_extra_41", "urx_extra_42", "usr_job_title",
    "usr_join_datetime", "eip_code", "urx_extra_44", "urx_extra_45",
    "userGroupId", "gradeId", "positionId", "tagId", "roleCode",
    "directLeaderId", "highestApprovalUserGroupId", "subordinateDepartmentId",
    "ssoConnectionEnable", "curLan",
}
FORBIDDEN_FIELDS = {
    "usr_pwd", "head_portrait_path", "eip_id", "pdate", "line", "userGroupCode",
    "usgDisplayBil", "gradeCode", "ugrDisplayBil", "positionCode", "positionTitle",
    "tag", "tagName", "userTagVOList", "directLeader", "directLeaderCode",
    "directLeaderDisplayBil", "leaderList", "highestApprovalUserGroupCode",
    "highestApprovalUsgDisplayBil", "subordinateDepartment",
    "subordinateDepartmentUsgDisplayBil", "usrSource", "usr_pwd_need_change_ind",
    "urx_extra_send_email", "noUpdLyndon",
}
READ_DEPENDENCIES = sorted(
    (EXPECTED_FIELDS - {"tagId", "directLeaderId", "curLan"})
    | {"userTagVOList", "leaderList"}
)
NULLABLE_FIELDS = (
    "positionId", "highestApprovalUserGroupId", "subordinateDepartmentId",
)
RELATIONS = (
    ("userTagVOList", "encryId", "tagId"),
    ("leaderList", "usr_ent_id_encrypt", "directLeaderId"),
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def read_model():
    # Genuine Allure response structure with redacted values, supplied by user.
    path = ROOT / "tests/fixtures/user_update_read_model.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def synthetic_relation_read_model():
    path = ROOT / "tests/fixtures/user_update_read_model_with_relations.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture["_fixture_metadata"]["synthetic"] is True
    return fixture["data"]


def build(read_model, **options):
    arguments = {
        "cur_lan": "en",
        "mutable_fields": {"usr_display_bil"},
        "overrides": {"usr_display_bil": "Test User Mutation"},
    }
    arguments.update(options)
    return UserUpdatePayloadBuilder.build(read_model, **arguments)


def test_complete_real_read_model_preserves_baseline(read_model):
    assert type(read_model["usr_ent_id"]) is int
    assert read_model["usr_ent_id"] == 1830
    assert read_model["positionId"] is None
    assert read_model["userTagVOList"] == []
    assert read_model["leaderList"] is None
    assert read_model["curLan"] is None
    assert read_model["usr_pwd"] == "<REDACTED>"
    plan = build(read_model, cur_lan="zh-cn")
    for field in EXPECTED_FIELDS - {"tagId", "directLeaderId", "curLan"}:
        assert plan.baseline_payload[field] == read_model[field]
        assert type(plan.baseline_payload[field]) is type(read_model[field])
    assert plan.baseline_payload["tagId"] is None
    assert plan.baseline_payload["directLeaderId"] is None
    assert plan.baseline_payload["curLan"] == "zh-cn"


def test_exact_safe_whitelist_and_changed_fields(read_model):
    plan = build(read_model)
    assert isinstance(plan, UserUpdatePayloadPlan)
    assert set(plan.baseline_payload) == EXPECTED_FIELDS
    assert set(plan.mutation_payload) == EXPECTED_FIELDS
    assert len(EXPECTED_FIELDS) == 27
    assert plan.baseline_payload["usr_display_bil"] == "Test User Baseline"
    assert plan.mutation_payload["usr_display_bil"] == "Test User Mutation"
    actual = {
        key for key in EXPECTED_FIELDS
        if plan.baseline_payload[key] != plan.mutation_payload[key]
    }
    assert actual == plan.changed_fields == {"usr_display_bil"}
    assert SUPPORTED_MUTABLE_FIELDS == {"usr_display_bil"}


@pytest.mark.parametrize("field", sorted(FORBIDDEN_FIELDS))
def test_forbidden_fields_never_emitted(read_model, field):
    if field not in {"userTagVOList", "leaderList"}:
        read_model[field] = "must-not-be-written"
    plan = build(read_model)
    for payload in (plan.baseline_payload, plan.mutation_payload):
        assert field not in payload
        assert not (set(payload) & FORBIDDEN_FIELDS)


def test_unknown_fields_and_stale_derived_values_are_ignored(synthetic_relation_read_model):
    read_model = synthetic_relation_read_model
    read_model.update({
        "futureWriteField": "unapproved", "tagId": "stale-tag",
        "directLeaderId": "stale-leader", "curLan": "stale-language",
    })
    plan = build(read_model, cur_lan="zh-HK")
    assert set(plan.baseline_payload) == EXPECTED_FIELDS
    assert plan.baseline_payload["curLan"] == "zh-HK"
    assert plan.baseline_payload["tagId"] == "synthetic-tag-b,synthetic-tag-a"
    assert plan.baseline_payload["directLeaderId"] == "synthetic-leader-b,synthetic-leader-a"


@pytest.mark.parametrize("field", READ_DEPENDENCIES)
def test_each_missing_dependency_fails_fast(read_model, field):
    del read_model[field]
    with pytest.raises(ValueError, match=field):
        build(read_model)


@pytest.mark.parametrize("field", NULLABLE_FIELDS)
@pytest.mark.parametrize("value", [None, "synthetic-relation-id"], ids=["null", "id"])
def test_explicit_nullable_relations_are_preserved(read_model, field, value):
    read_model[field] = value
    plan = build(read_model)
    assert plan.baseline_payload[field] == value
    assert plan.mutation_payload[field] == value


@pytest.mark.parametrize("field", NULLABLE_FIELDS)
@pytest.mark.parametrize("value", ["", " \t", 0, False, [], {}])
def test_nullable_relations_reject_non_null_empty_or_invalid_ids(read_model, field, value):
    read_model[field] = value
    with pytest.raises(ValueError, match=field):
        build(read_model)


OPTIONAL_DIRECT_FIELDS = sorted(
    EXPECTED_FIELDS - {
        "usr_ent_id", "usr_ste_usr_id", "usr_display_bil", "userGroupId",
        "gradeId", "roleCode", "ssoConnectionEnable", "curLan", "tagId",
        "directLeaderId", "usr_gender", *NULLABLE_FIELDS,
    }
)


@pytest.mark.parametrize("field", OPTIONAL_DIRECT_FIELDS)
@pytest.mark.parametrize("value", [None, ""], ids=["null", "empty"])
def test_optional_scalars_preserve_null_and_empty_string(read_model, field, value):
    read_model[field] = value
    plan = build(read_model)
    assert plan.baseline_payload[field] == value
    assert plan.mutation_payload[field] == value
    assert type(plan.baseline_payload[field]) is type(value)
    assert type(plan.mutation_payload[field]) is type(value)


@pytest.mark.parametrize("value", [1, 1830, "1830"], ids=["positive-int", "real-int-1830", "compat-decimal-string"])
def test_valid_raw_user_entity_ids_preserve_original_type(read_model, value):
    read_model["usr_ent_id"] = value
    actual = build(read_model).baseline_payload["usr_ent_id"]
    assert actual == value
    assert type(actual) is type(value)


@pytest.mark.parametrize("value", ["M", "F", ""], ids=["male", "female", "empty"])
def test_gender_accepts_only_read_model_values(read_model, value):
    read_model["usr_gender"] = value
    assert build(read_model).baseline_payload["usr_gender"] == value


@pytest.mark.parametrize("value", [None, "m", "Unknown", " ", 0, False, [], {}])
def test_invalid_gender_fails_fast(read_model, value):
    read_model["usr_gender"] = value
    with pytest.raises(ValueError, match="usr_gender"):
        build(read_model)


@pytest.mark.parametrize("value", [None, "", " ", 0, -1, "0", "-1", "abc", 1.5, True, [], {}], ids=["null", "empty", "blank", "zero-int", "negative-int", "zero-string", "negative-string", "opaque-string", "float", "boolean", "list", "mapping"])
def test_invalid_user_entity_ids_fail_fast(read_model, value):
    read_model["usr_ent_id"] = value
    with pytest.raises(ValueError, match="usr_ent_id"):
        build(read_model)


@pytest.mark.parametrize("field", ["usr_ste_usr_id", "usr_display_bil", "roleCode"])
@pytest.mark.parametrize("value", [None, "", " \t", 1, True, [], {}], ids=["null", "empty", "blank", "integer", "boolean", "list", "mapping"])
def test_required_text_fields_fail_fast(read_model, field, value):
    read_model[field] = value
    with pytest.raises(ValueError, match=field):
        build(read_model)


@pytest.mark.parametrize("field", ["userGroupId", "gradeId"])
@pytest.mark.parametrize("value", [None, "", " \t", 0, -1, True, 1.5, [], {}], ids=["null", "empty", "blank", "zero", "negative", "boolean", "float", "list", "mapping"])
def test_required_relation_ids_fail_fast(read_model, field, value):
    read_model[field] = value
    with pytest.raises(ValueError, match=field):
        build(read_model)


@pytest.mark.parametrize("field", ["userGroupId", "gradeId"])
@pytest.mark.parametrize("value", [7, "synthetic-encoded-id"])
def test_required_relation_ids_are_preserved(read_model, field, value):
    read_model[field] = value
    assert build(read_model).baseline_payload[field] == value


@pytest.mark.parametrize("value", ["Yes", "No"])
def test_exact_sso_values_pass(read_model, value):
    read_model["ssoConnectionEnable"] = value
    assert build(read_model).baseline_payload["ssoConnectionEnable"] == value


@pytest.mark.parametrize("value", [None, "", "yes", "NO", " Yes ", True, 1, [], {}], ids=["null", "empty", "lowercase", "uppercase", "padded", "boolean", "integer", "list", "mapping"])
def test_other_sso_values_fail_fast(read_model, value):
    read_model["ssoConnectionEnable"] = value
    with pytest.raises(ValueError, match="ssoConnectionEnable"):
        build(read_model)


@pytest.mark.parametrize("value", ["A", "A" * 80, "名" * 80])
def test_display_name_valid_boundaries(read_model, value):
    read_model["usr_display_bil"] = value
    plan = build(read_model, overrides={"usr_display_bil": value + "B" if len(value) < 80 else "B"})
    assert plan.baseline_payload["usr_display_bil"] == value


def test_baseline_display_name_over_80_fails(read_model):
    read_model["usr_display_bil"] = "A" * 81
    with pytest.raises(ValueError, match="usr_display_bil"):
        build(read_model)


@pytest.mark.parametrize("field,id_field,output", RELATIONS, ids=["tags", "leaders"])
@pytest.mark.parametrize("identifiers", [[], ["only"], ["z", "a", "m"]], ids=["empty", "single", "multiple"])
def test_relation_derivation_keeps_order(read_model, field, id_field, output, identifiers):
    read_model[field] = [{id_field: identifier} for identifier in identifiers]
    expected = ",".join(identifiers) if identifiers else None
    plan = build(read_model)
    assert plan.baseline_payload[output] == expected
    assert plan.mutation_payload[output] == expected


def test_synthetic_relation_fixture_keeps_original_order(synthetic_relation_read_model):
    plan = build(synthetic_relation_read_model)
    assert plan.baseline_payload["tagId"] == "synthetic-tag-b,synthetic-tag-a"
    assert plan.baseline_payload["directLeaderId"] == "synthetic-leader-b,synthetic-leader-a"


def test_null_leader_list_derives_null(read_model):
    read_model["leaderList"] = None
    assert build(read_model).baseline_payload["directLeaderId"] is None


@pytest.mark.parametrize("field", ["userTagVOList", "leaderList"])
@pytest.mark.parametrize("value", ["", {}, "id", 1, ("id",)])
def test_wrong_relation_container_type_fails(read_model, field, value):
    read_model[field] = value
    with pytest.raises(ValueError, match=field):
        build(read_model)


def test_null_tag_list_fails_instead_of_becoming_empty(read_model):
    read_model["userTagVOList"] = None
    with pytest.raises(ValueError, match="userTagVOList"):
        build(read_model)


@pytest.mark.parametrize("field,id_field,output", RELATIONS)
@pytest.mark.parametrize("value", [None, "", " \t", 7, True, [], {}])
def test_relation_element_invalid_id_fails(read_model, field, id_field, output, value):
    read_model[field] = [{id_field: value}]
    with pytest.raises(ValueError, match=id_field):
        build(read_model)


@pytest.mark.parametrize("field,id_field,output", RELATIONS, ids=["tags", "leaders"])
@pytest.mark.parametrize("entry", [{}, None, "id", 7, []], ids=["missing-id", "null-entry", "string-entry", "numeric-entry", "list-entry"])
def test_relation_element_missing_id_or_wrong_type_fails(read_model, field, id_field, output, entry):
    read_model[field] = [entry]
    with pytest.raises(ValueError, match=id_field):
        build(read_model)


@pytest.mark.parametrize("field,id_field,output", RELATIONS, ids=["tags", "leaders"])
@pytest.mark.parametrize("identifiers", [["a", "a"], ["a", "b", "a"]], ids=["adjacent", "nonadjacent"])
def test_duplicate_relation_ids_fail_fast(read_model, field, id_field, output, identifiers):
    read_model[field] = [{id_field: identifier} for identifier in identifiers]
    with pytest.raises(ValueError, match="duplicate"):
        build(read_model)


@pytest.mark.parametrize("mutable_fields", [{"roleCode"}, {"usr_email"}, {"usr_pwd"}, {"usr_display_bil", "roleCode"}], ids=["role", "email", "password", "mixed"])
def test_unsupported_mutable_fields_fail(read_model, mutable_fields):
    with pytest.raises(ValueError, match="unsupported"):
        build(read_model, mutable_fields=mutable_fields, overrides={field: "override" for field in mutable_fields})


@pytest.mark.parametrize("mutable_fields,overrides", [
    ({"usr_display_bil"}, {}),
    ({"usr_display_bil"}, {"roleCode": "override"}),
    ({"usr_display_bil"}, {"usr_display_bil": "New", "roleCode": "override"}),
    (set(), {"usr_display_bil": "New"}),
    ({"usr_display_bil"}, {"usr_display_bil": "New", "usr_email": "override"}),
    ({"usr_display_bil"}, {"usr_display_bil": "New", "usr_pwd": "override"}),
    ({"usr_display_bil"}, {"usr_display_bil": "New", "head_portrait_path": "override"}),
], ids=["missing", "wrong-field", "extra-role", "empty-mutable", "email", "password", "portrait"])
def test_override_keys_must_exactly_match_mutable_fields(read_model, mutable_fields, overrides):
    with pytest.raises(ValueError, match="overrides keys"):
        build(read_model, mutable_fields=mutable_fields, overrides=overrides)


@pytest.mark.parametrize("value", [None, "", " \t", 1, True, [], {}, "A" * 81], ids=["null", "empty", "blank", "integer", "boolean", "list", "mapping", "over-80"])
def test_invalid_mutation_display_name_fails(read_model, value):
    with pytest.raises(ValueError, match="usr_display_bil"):
        build(read_model, overrides={"usr_display_bil": value})


def test_unchanged_override_fails_actual_changed_fields_guard(read_model):
    with pytest.raises(ValueError, match="actual_changed_fields"):
        build(read_model, overrides={"usr_display_bil": read_model["usr_display_bil"]})


@pytest.mark.parametrize("value", ["N", "N" * 80, "名" * 80])
def test_valid_mutation_boundaries_and_only_requested_change(read_model, value):
    plan = build(read_model, mutable_fields=frozenset({"usr_display_bil"}), overrides={"usr_display_bil": value})
    assert plan.mutation_payload["usr_display_bil"] == value
    assert plan.changed_fields == {"usr_display_bil"}
    for field in EXPECTED_FIELDS - {"usr_display_bil"}:
        assert plan.mutation_payload[field] == plan.baseline_payload[field]


def test_empty_mutation_is_valid_and_independently_copied(read_model):
    plan = build(read_model, mutable_fields=set(), overrides={})
    assert plan.changed_fields == set()
    assert plan.baseline_payload == plan.mutation_payload
    assert plan.baseline_payload is not plan.mutation_payload


def test_cur_lan_argument_is_required(read_model):
    with pytest.raises(TypeError, match="cur_lan"):
        UserUpdatePayloadBuilder.build(
            read_model, mutable_fields={"usr_display_bil"},
            overrides={"usr_display_bil": "New Name"},
        )


@pytest.mark.parametrize("value", [None, "", " ", 1, True], ids=["null", "empty", "blank", "integer", "boolean"])
def test_cur_lan_must_be_nonblank_text(read_model, value):
    with pytest.raises(ValueError, match="curLan"):
        build(read_model, cur_lan=value)


@pytest.mark.parametrize("value", ["NLRN_1,TADM_1", "TADM_1,NLRN_1"], ids=["original", "reversed"])
def test_multiple_role_codes_keep_original_order(read_model, value):
    read_model["roleCode"] = value
    plan = build(read_model)
    assert plan.baseline_payload["roleCode"] == value
    assert plan.mutation_payload["roleCode"] == value


@pytest.mark.parametrize("value", [",NLRN_1", "NLRN_1,", "NLRN_1,,TADM_1", "NLRN_1, \t,TADM_1"], ids=["leading", "trailing", "middle", "blank"])
def test_role_code_empty_tokens_fail_fast(read_model, value):
    read_model["roleCode"] = value
    with pytest.raises(ValueError, match="roleCode token"):
        build(read_model)


@pytest.mark.parametrize("value", ["NLRN_1,NLRN_1", "NLRN_1,TADM_1,NLRN_1", "NLRN_1, NLRN_1"], ids=["adjacent", "nonadjacent", "whitespace"])
def test_role_code_duplicate_tokens_fail_fast(read_model, value):
    read_model["roleCode"] = value
    with pytest.raises(ValueError, match="roleCode.*duplicate"):
        build(read_model)


@pytest.mark.parametrize("argument,value,error", [
    ("read_model", None, "read_model"),
    ("read_model", [], "read_model"),
    ("mutable_fields", "usr_display_bil", "mutable_fields"),
    ("mutable_fields", ["usr_display_bil"], "mutable_fields"),
    ("mutable_fields", None, "mutable_fields"),
    ("overrides", None, "overrides"),
    ("overrides", [], "overrides"),
])
def test_invalid_argument_shapes_fail(read_model, argument, value, error):
    if argument == "read_model":
        read_model = value
        options = {}
    else:
        options = {argument: value}
    with pytest.raises(ValueError, match=error):
        build(read_model, **options)


def test_success_and_failure_do_not_modify_inputs(read_model):
    original = deepcopy(read_model)
    mutable_fields = {"usr_display_bil"}
    overrides = {"usr_display_bil": "New Name"}
    build(read_model, mutable_fields=mutable_fields, overrides=overrides)
    assert read_model == original
    assert mutable_fields == {"usr_display_bil"}
    assert overrides == {"usr_display_bil": "New Name"}
    with pytest.raises(ValueError):
        build(read_model, overrides={"usr_display_bil": original["usr_display_bil"]})
    assert read_model == original


def test_baseline_mutation_and_input_are_independent_deep_copies(read_model):
    # Nested JSON exposes shallow-copy bugs even in otherwise scalar fields.
    read_model["usr_extra_2"] = {"items": [{"value": "original"}]}
    original = deepcopy(read_model)
    plan = build(read_model)
    plan.baseline_payload["usr_extra_2"]["items"][0]["value"] = "baseline-only"
    assert plan.mutation_payload["usr_extra_2"] == original["usr_extra_2"]
    assert read_model == original
    plan.mutation_payload["usr_extra_2"]["items"].append({"value": "mutation-only"})
    assert len(plan.baseline_payload["usr_extra_2"]["items"]) == 1
    assert read_model == original
    read_model["usr_extra_2"]["items"][0]["value"] = "input-only"
    assert plan.mutation_payload["usr_extra_2"]["items"][0]["value"] == "original"


def test_builder_is_deterministic_across_repeated_calls(read_model):
    assert build(read_model) == build(read_model)
    first, second = build(read_model), build(read_model)
    first.baseline_payload["usr_email"] = "first-only@example.invalid"
    assert second.baseline_payload["usr_email"] == read_model["usr_email"]


def test_builder_loads_and_runs_without_framework_or_network_dependencies(read_model):
    # Isolated interpreter excludes pytest/conftest and all framework imports.
    script = '''
import importlib.abc
import json
import runpy
import sys

class OfflineOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"requests", "http", "urllib", "socket", "wizbank_api_test"}:
            raise AssertionError("Forbidden dependency: " + fullname)

sys.meta_path.insert(0, OfflineOnly())
module = runpy.run_path(sys.argv[1])
plan = module["UserUpdatePayloadBuilder"].build(
    json.loads(sys.stdin.read()), cur_lan="en",
    mutable_fields={"usr_display_bil"}, overrides={"usr_display_bil": "Isolated Mutation"},
)
assert plan.changed_fields == {"usr_display_bil"}
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT / "services/user_update_payload_builder.py")],
        input=json.dumps(read_model), text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
