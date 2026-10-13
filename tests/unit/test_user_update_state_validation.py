from copy import deepcopy
import json
from pathlib import Path

import pytest

from wizbank_api_test.services import user_update_state_validation as state
from wizbank_api_test.services.user_update_payload_builder import UserUpdatePayloadBuilder


@pytest.fixture
def model():
    path = Path(__file__).resolve().parents[2] / "tests/fixtures/user_update_read_model.json"
    return json.loads(path.read_text())


def test_projection_uses_frozen_no_change_builder_and_preserves_input(model, monkeypatch):
    original = deepcopy(model)
    build, calls = UserUpdatePayloadBuilder.build, []

    def observed(read_model, **kwargs):
        calls.append(kwargs)
        return build(read_model, **kwargs)

    monkeypatch.setattr(UserUpdatePayloadBuilder, "build", observed)
    projected = state.project(model, "zh-cn")
    assert calls == [{"cur_lan": "zh-cn", "mutable_fields": set(), "overrides": {}}]
    assert len(projected) == 27
    assert projected["curLan"] == "zh-cn" and model["curLan"] is None
    assert model == original
    assert "usr_pwd" not in projected and "userTagVOList" not in projected


@pytest.mark.parametrize("relation", ["roleCode", "tagId", "directLeaderId"])
def test_relation_order_and_whitespace_do_not_create_diff(model, relation):
    before = state.normalize(model, "zh-cn")
    after = deepcopy(before)
    before[relation], after[relation] = "second, first", " first ,second "
    assert state.diff(state.canonicalize(before), state.canonicalize(after)) == {}


@pytest.mark.parametrize("read_field,id_field", [
    ("userTagVOList", "encryId"), ("leaderList", "usr_ent_id_encrypt"),
])
def test_projection_compares_unordered_read_model_relations(model, read_field, id_field):
    before = deepcopy(model)
    before[read_field] = [{id_field: "id-a"}, {id_field: "id-b"}]
    after = deepcopy(before)
    after[read_field].reverse()
    assert state.diff(state.project(before, "zh-cn"), state.project(after, "zh-cn")) == {}


@pytest.mark.parametrize("relation", ["tagId", "directLeaderId"])
def test_nullable_relation_empty_representations_are_equivalent(model, relation):
    before = state.normalize(model, "zh-cn")
    after = deepcopy(before)
    before[relation], after[relation] = None, ""
    assert state.diff(state.canonicalize(before), state.canonicalize(after)) == {}


@pytest.mark.parametrize("field,value", [
    ("usr_nickname", None), ("usr_bday", ""), ("usr_bday", "2000-01-01"),
    ("ssoConnectionEnable", "Yes"), ("positionId", "position-other"),
    ("userGroupId", "group-other"), ("gradeId", "grade-other"),
    ("usr_ent_id", "1830"), ("usr_email", "changed@example.invalid"),
])
def test_scalar_drift_is_strict_and_fails_restore(model, field, value):
    before = state.project(model, "zh-cn")
    after = deepcopy(before)
    after[field] = value
    assert set(state.diff(before, after)) == {field}
    with pytest.raises(AssertionError, match=field):
        state.assert_restored(before, after)


def test_display_mutation_only_and_identical_restore(model):
    before = state.project(model, "zh-cn")
    after = deepcopy(before)
    after["usr_display_bil"] = "offline changed display"
    assert set(state.assert_mutation_only(before, after)) == {"usr_display_bil"}
    assert state.assert_restored(before, deepcopy(before)) == {}


@pytest.mark.parametrize("field", ["userGroupId", "gradeId", "roleCode", "tagId", "directLeaderId"])
def test_non_target_drift_fails_mutation_only(model, field):
    before = state.project(model, "zh-cn")
    after = deepcopy(before)
    after["usr_display_bil"] = "offline changed display"
    after[field] = ("different",) if field in state.RELATION_FIELDS else "different"
    with pytest.raises(AssertionError, match=field):
        state.assert_mutation_only(before, after)


def test_no_mutation_is_not_a_success(model):
    before = state.project(model, "zh-cn")
    with pytest.raises(AssertionError):
        state.assert_mutation_only(before, before)


def test_diff_results_do_not_alias_inputs(model):
    before, after = state.project(model, "zh-cn"), state.project(model, "zh-cn")
    before["usr_extra_2"], after["usr_extra_2"] = ["before"], ["after"]
    changes = state.diff(before, after)
    changes["usr_extra_2"]["before"].append("external change")
    assert before["usr_extra_2"] == ["before"]


def test_scalar_types_are_not_coerced(model):
    before = state.project(model, "zh-cn")
    after = deepcopy(before)
    before["usr_extra_2"], after["usr_extra_2"] = 1, True
    assert set(state.diff(before, after)) == {"usr_extra_2"}
