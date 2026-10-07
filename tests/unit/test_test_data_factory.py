import os

import pytest

from wizbank_api_test.core.context import Context
from wizbank_api_test.core.variable_resolver import VariableResolver


@pytest.fixture(autouse=True)
def reset_context():
    Context.clear()
    yield
    Context.clear()


def test_data_test_username_reads_environment(monkeypatch):
    monkeypatch.setenv("TEST_USERNAME", "ee-test-user")

    actual = VariableResolver.resolve("{{$data.test_username}}")

    assert actual == "ee-test-user"
    assert Context.get_variable("data.test_username") == "ee-test-user"


def test_unique_display_name_is_cached_for_same_run(monkeypatch):
    monkeypatch.setenv("TEST_PREFIX", "wb")

    first = VariableResolver.resolve("{{$data.unique_display_name}}")
    second = VariableResolver.resolve("{{$data.unique_display_name}}")

    assert first == second
    assert first.startswith("wb-disp-")
    assert Context.get_variable("data.unique_display_name") == first


def test_unknown_data_key_fails_fast():
    with pytest.raises(ValueError, match="不支持的测试数据键"):
        VariableResolver.resolve("{{$data.unknown_value}}")


def test_data_reference_can_be_used_inside_extract_rule(monkeypatch):
    monkeypatch.setenv("TEST_USERNAME", "ee-test-user")

    rule = {
        "target_usr_ent_id": {
            "type": "find_in_list",
            "source": "$.data.list",
            "where": {
                "field": "usr_ste_usr_id",
                "equals": "{{$data.test_username}}",
            },
            "get": "usr_ent_id_encrypt",
        }
    }

    resolved = VariableResolver.resolve(rule)

    assert (
        resolved["target_usr_ent_id"]["where"]["equals"]
        == "ee-test-user"
    )
