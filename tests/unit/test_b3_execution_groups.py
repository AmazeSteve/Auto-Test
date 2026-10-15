"""Pure ownership grouping preserves the directed, order-based YAML workflow."""
from copy import deepcopy
from itertools import permutations
from pathlib import Path

import pytest

from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.case_selector import CaseSelector


def case(case_id, order=9999, **fields):
    return {"id": case_id, "order": order, **fields}


def test_independent_cases_have_distinct_groups():
    assert CaseSelector.build_execution_groups([case("B"), case("A")]) == {
        "A": "A", "B": "B",
    }


@pytest.mark.parametrize("depends_on", ["A", ["A"]])
def test_direct_and_transitive_dependencies_share_one_group(depends_on):
    cases = [case("A", 1), case("B", 2, depends_on=depends_on), case("C", 3, depends_on=["B"])]
    assert CaseSelector.build_execution_groups(cases) == {"A": "A", "B": "A", "C": "A"}


def test_dependency_branches_share_common_predecessor():
    cases = [case("A", 1), case("B", 2, depends_on="A"), case("C", 3, depends_on="A")]
    assert CaseSelector.build_execution_groups(cases) == {"A": "A", "B": "A", "C": "A"}


def test_multiple_dependencies_merge_ownership_components():
    cases = [case("A", 1), case("B", 2), case("C", 3, depends_on=["A", "B"])]
    assert CaseSelector.build_execution_groups(cases) == {"A": "A", "B": "A", "C": "A"}


def test_lifecycle_edges_group_all_phases_without_depends_on():
    cases = [
        case("ROOT", 1, lifecycle={"verify": "VERIFY", "rollback": "ROLLBACK", "verify_rollback": "RESTORED"}),
        case("VERIFY", 2), case("ROLLBACK", 3), case("RESTORED", 4),
    ]
    assert set(CaseSelector.build_execution_groups(cases).values()) == {"ROOT"}


def test_lifecycle_group_also_contains_root_predecessors():
    cases = [
        case("READ", 1),
        case("ROOT", 2, depends_on="READ", lifecycle={"verify": "VERIFY", "rollback": "ROLLBACK", "verify_rollback": "RESTORED"}),
        case("VERIFY", 3), case("ROLLBACK", 4), case("RESTORED", 5),
    ]
    assert set(CaseSelector.build_execution_groups(cases).values()) == {"READ"}


def test_disconnected_dependency_chains_have_separate_groups():
    cases = [case("A", 1), case("B", 2, depends_on="A"), case("X", 1), case("Y", 2, depends_on="X"), case("Z", 3)]
    assert CaseSelector.build_execution_groups(cases) == {"A": "A", "X": "X", "B": "A", "Y": "X", "Z": "Z"}


def test_all_input_permutations_produce_identical_mapping_and_iteration_order():
    cases = [case("Z", 3), case("B", 1), case("A", 1, depends_on="B"), case("C", 2, depends_on="A")]
    expected = {"A": "A", "B": "A", "C": "A", "Z": "Z"}
    for shuffled in permutations(cases):
        actual = CaseSelector.build_execution_groups(list(shuffled))
        assert actual == expected
        assert list(actual) == list(expected)


def test_group_id_uses_order_before_lexical_id():
    cases = [case("A", 20, depends_on="Z"), case("Z", 10)]
    assert CaseSelector.build_execution_groups(cases) == {"Z": "Z", "A": "Z"}


def test_default_order_9999_participates_in_group_id_selection():
    cases = [{"id": "A", "depends_on": "Z"}, case("Z", 9998)]
    assert CaseSelector.build_execution_groups(cases) == {"Z": "Z", "A": "Z"}


def test_grouping_does_not_mutate_cases_or_directed_dependency_closure():
    cases = [case("A", 1, extract={"target": "$.data.id"}), case("B", 2, depends_on=["A"])]
    original = deepcopy(cases)
    CaseSelector.build_execution_groups(cases)
    case_map = CaseSelector.build_case_map(cases)
    assert cases == original
    assert CaseSelector.collect_dependency_ids(case_map["A"], case_map) == set()
    assert CaseSelector.collect_dependency_ids(case_map["B"], case_map) == {"A"}


def test_empty_input_has_no_groups():
    assert CaseSelector.build_execution_groups([]) == {}


def test_out_of_scope_references_do_not_create_phantom_groups():
    cases = [case("A", 1, depends_on="OUTSIDE"), case("B", 2)]
    assert CaseSelector.build_execution_groups(cases) == {"A": "A", "B": "B"}


def test_real_yaml_dependency_and_lifecycle_chains_form_separate_components():
    root = Path(__file__).resolve().parents[2]
    cases = CaseLoader.load_cases(root / "data/cases", root / "data/endpoints")
    groups = CaseSelector.build_execution_groups(cases)
    user_group = groups["WB_USER_GROUP_001"]
    for case_id in (
        "WB_USER_GROUP_004", "WB_USER_004", "WB_USER_DESTRUCTIVE_DEMO_001",
        "WB_USER_DESTRUCTIVE_VERIFY_001", "WB_USER_DESTRUCTIVE_ROLLBACK_001",
        "WB_USER_DESTRUCTIVE_VERIFY_ROLLBACK_001",
    ):
        assert groups[case_id] == user_group
    assert groups["WB_USER_REF_003"] == groups["WB_USER_SEARCH_001"]
    assert groups["WB_USER_REF_003"] != user_group


def test_real_contract_experiment_shares_its_dependency_group():
    root = Path(__file__).resolve().parents[2]
    cases = CaseLoader.load_cases(root / "data/cases", root / "data/endpoints")
    cases += CaseLoader.load_cases(root / "data/experiments", root / "data/endpoints")
    groups = CaseSelector.build_execution_groups(cases)
    for suffix in ("PARTIAL", "VERIFY", "ROLLBACK", "VERIFY_ROLLBACK"):
        assert groups[f"WB_USER_UPDATE_CONTRACT_{suffix}_001"] == groups["WB_USER_004"]
