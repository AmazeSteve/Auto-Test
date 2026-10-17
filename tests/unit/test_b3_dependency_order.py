"""Static order contract complements the existing dependency validators."""
from pathlib import Path

import pytest

from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.case_validator import CaseValidator
from wizbank_api_test.core.endpoint_registry import EndpointRegistry


@pytest.fixture(scope="module", autouse=True)
def initialized_endpoint_registry():
    previous = EndpointRegistry._instance
    EndpointRegistry._instance = None
    root = Path(__file__).resolve().parents[2]

    try:
        EndpointRegistry(root / "data/endpoints")
        yield
    finally:
        EndpointRegistry._instance = previous


def case(case_id, order=9999, **fields):
    return {"id": case_id, "name": case_id, "method": "GET", "path": "/offline", "auth": "none", "order": order, **fields}


def test_lower_order_dependency_is_valid_regardless_of_input_order():
    cases = [case("A", 20, depends_on="B"), case("B", 10)]
    assert CaseValidator._validate_dependency_order(cases) == []
    CaseValidator.validate_all(cases)


@pytest.mark.parametrize("dependency_order", [20, 21])
def test_same_or_higher_dependency_order_fails_before_transport(dependency_order):
    cases = [case("A", 20, depends_on=["B"]), case("B", dependency_order)]
    transport_calls = []
    with pytest.raises(ValueError, match=r"order\(B\) < order\(A\)"):
        CaseValidator.validate_all(cases)
        transport_calls.append("would request")
    assert transport_calls == []


def test_each_dependency_must_precede_its_consumer():
    cases = [case("A", 20, depends_on=["B", "C"]), case("B", 10), case("C", 20)]
    errors = CaseValidator._validate_dependency_order(cases)
    assert len(errors) == 1
    assert "order(C) < order(A)" in errors[0]


def test_missing_consumer_order_uses_loader_default_9999():
    cases = [case("A", depends_on="B"), case("B", 9998)]
    cases[0].pop("order")
    assert CaseValidator._validate_dependency_order(cases) == []
    CaseValidator.validate_all(cases)


def test_missing_dependency_order_uses_loader_default_9999():
    cases = [case("A", 10000, depends_on="B"), case("B")]
    cases[1].pop("order")
    assert CaseValidator._validate_dependency_order(cases) == []
    CaseValidator.validate_all(cases)


def test_both_default_orders_fail_strict_order_contract():
    cases = [case("A", depends_on="B"), case("B")]
    for item in cases:
        item.pop("order")
    with pytest.raises(ValueError, match="9999 >= 9999"):
        CaseValidator.validate_all(cases)


@pytest.mark.parametrize("invalid_order", ["10", None, [], {}])
@pytest.mark.parametrize("invalid_case_id", ["A", "B"])
def test_invalid_order_types_remain_owned_by_existing_order_validator(invalid_order, invalid_case_id):
    cases = [case("A", 20, depends_on="B"), case("B", 10)]
    next(item for item in cases if item["id"] == invalid_case_id)["order"] = invalid_order
    assert CaseValidator._validate_dependency_order(cases) == []
    assert len(CaseValidator._validate_order(cases)) == 1
    with pytest.raises(ValueError, match="order 必须是 int") as error:
        CaseValidator.validate_all(cases)
    assert "depends_on requires order" not in str(error.value)


def test_missing_dependency_keeps_one_existing_missing_reference_diagnostic():
    cases = [case("A", 10, depends_on="MISSING")]
    assert len(CaseValidator._validate_depends_on_exists(cases)) == 1
    assert CaseValidator._validate_dependency_order(cases) == []
    with pytest.raises(ValueError) as error:
        CaseValidator.validate_all(cases)
    assert str(error.value).count("depends_on 引用了不存在的用例") == 1
    assert "depends_on requires order" not in str(error.value)


def test_self_dependency_keeps_existing_self_and_cycle_diagnostics_without_order_duplicate():
    cases = [case("A", 10, depends_on="A")]
    assert len(CaseValidator._validate_self_dependency(cases)) == 1
    assert len(CaseValidator._validate_cycle_dependency(cases)) == 1
    assert CaseValidator._validate_dependency_order(cases) == []
    with pytest.raises(ValueError) as error:
        CaseValidator.validate_all(cases)
    assert str(error.value).count("不允许依赖自己") == 1
    assert str(error.value).count("检测到循环依赖") == 1
    assert "depends_on requires order" not in str(error.value)


def test_directed_cycle_remains_reported_by_existing_cycle_validator_once():
    cases = [case("A", 10, depends_on="B"), case("B", 20, depends_on="A")]
    assert len(CaseValidator._validate_cycle_dependency(cases)) == 1
    with pytest.raises(ValueError) as error:
        CaseValidator.validate_all(cases)
    assert str(error.value).count("检测到循环依赖") == 1


def test_invalid_dependency_type_remains_existing_validator_diagnostic():
    cases = [case("A", 10, depends_on=42)]
    assert CaseValidator._validate_dependency_order(cases) == []
    with pytest.raises(ValueError) as error:
        CaseValidator.validate_all(cases)
    assert str(error.value).count("depends_on 类型必须是 str 或 list") == 1


@pytest.mark.parametrize("include_experiments,expected_count", [(False, 28), (True, 32)])
def test_current_real_yaml_passes_complete_static_validation(include_experiments, expected_count):
    root = Path(__file__).resolve().parents[2]
    cases = CaseLoader.load_cases(root / "data/cases", root / "data/endpoints")
    if include_experiments:
        cases += CaseLoader.load_cases(root / "data/experiments", root / "data/endpoints")
    assert len(cases) == expected_count
    assert CaseValidator._validate_dependency_order(cases) == []
    CaseValidator.validate_all(cases)
