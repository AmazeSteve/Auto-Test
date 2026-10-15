"""Workflow ownership using the real YAML entrypoint fixtures and runners."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import wizbank_api_test

from wizbank_api_test.core.context import Context
from wizbank_api_test.core.runtime_context import bind_runtime_context
from wizbank_api_test.tests import test_03_api_cases as yaml_entry
from wizbank_api_test.tests import test_04_user_update_contract as contract_entry


ENTRIES = [yaml_entry, contract_entry]


def _case_request(case_id):
    return SimpleNamespace(
        node=SimpleNamespace(callspec=SimpleNamespace(params={"case_id": case_id})),
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=["generic", "contract"])
def test_module_registry_discards_groups_on_teardown(entry):
    registry_fixture = entry.workflow_runtime_contexts.__wrapped__()
    registry = next(registry_fixture)
    case_id = entry.ALL_CASES[0]["id"]
    runtime = entry.runtime_context.__wrapped__(_case_request(case_id), registry)
    assert registry and runtime in registry.values()

    registry_fixture.close()

    assert registry == {}


def test_same_group_id_in_generic_and_contract_modules_is_not_shared():
    generic_fixture = yaml_entry.workflow_runtime_contexts.__wrapped__()
    contract_fixture = contract_entry.workflow_runtime_contexts.__wrapped__()
    generic_registry = next(generic_fixture)
    contract_registry = next(contract_fixture)
    shared_case_id = yaml_entry.ALL_CASES[0]["id"]
    assert shared_case_id in contract_entry.CASE_MAP
    try:
        generic = yaml_entry.runtime_context.__wrapped__(
            _case_request(shared_case_id), generic_registry,
        )
        contract = contract_entry.runtime_context.__wrapped__(
            _case_request(shared_case_id), contract_registry,
        )
        assert set(generic_registry) == set(contract_registry)
        assert generic is not contract
        with bind_runtime_context(generic):
            Context.set_variable("module_owner", "generic-only")
            Context.mark_case_passed(shared_case_id)
            Context.mark_effect(shared_case_id, state=Context.EFFECT_APPLIED)
        with bind_runtime_context(contract):
            assert Context.all() == {"variables": {}, "cases": {}, "effects": {}}
    finally:
        generic_fixture.close()
        contract_fixture.close()
    assert generic_registry == contract_registry == {}


_NESTED_WORKFLOW = '''
import json
from pathlib import Path
from urllib.parse import urlsplit

import allure
import pytest
import requests
from requests.adapters import BaseAdapter

from wizbank_api_test.core.client import ApiClient
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.runtime_context import get_runtime_context
from wizbank_api_test.tests import {module_name} as entry

runtime_context = entry.runtime_context
workflow_runtime_contexts = entry.workflow_runtime_contexts

CASES = [
    {{"id": "PREDECESSOR", "name": "predecessor", "order": 10,
     "method": "GET", "path": "/producer", "auth": "none",
     "extract": {{"predecessor_value": {{"type": "json_path", "expression": "$.value"}}}}}},
    {{"id": "DEPENDENT", "name": "dependent", "order": 20,
     "method": "GET", "path": "/consumer/{{{{predecessor_value}}}}", "auth": "none",
     "depends_on": ["PREDECESSOR"]}},
    {{"id": "INDEPENDENT", "name": "independent", "order": 30,
     "method": "GET", "path": "/independent", "auth": "none"}},
]
entry.ALL_CASES = CASES
entry.CASE_MAP = {{case["id"]: case for case in CASES}}
entry.LIFECYCLE_CHILD_MAP = {{}}
requests_seen = []
attachments = []
contexts_seen = {{}}
registry_seen = None


class OfflineAdapter(BaseAdapter):
    def send(self, request, **kwargs):
        requests_seen.append(request.url)
        assert request.method == "GET"
        assert "Cookie" not in request.headers and "Authorization" not in request.headers
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({{"value": "workflow-canary"}}).encode()
        return response

    def close(self):
        pass


@pytest.fixture(scope="module", autouse=True)
def audit_module_teardown():
    yield
    assert registry_seen == {{}}, "module registry retained workflow contexts"
    with pytest.raises(RuntimeError, match="No active RuntimeContext"):
        get_runtime_context()
    Path({report_path!r}).write_text(json.dumps({{
        "requests": requests_seen,
        "contexts": contexts_seen,
        "registry_discarded": registry_seen == {{}},
        "binding_restored": True,
    }}), encoding="utf-8")


@pytest.fixture
def api_client_factory():
    def factory(host_key="Host-be", principal="none"):
        assert principal == "none"
        client = ApiClient("https://workflow.invalid", host_key=host_key, principal=principal)
        client.session.mount("https://", OfflineAdapter())
        return client
    return factory


@pytest.fixture
def credential_provider():
    class NoCredentials:
        def get(self, *args):
            pytest.fail("anonymous workflow cannot request credentials")
        def assert_client_matches(self, *args):
            pytest.fail("anonymous workflow cannot inspect credential ownership")
    return NoCredentials()


@pytest.mark.parametrize("case_id", ["PREDECESSOR", "DEPENDENT", "INDEPENDENT"])
def test_workflow_item(case_id, api_client_factory, credential_provider, request,
                       workflow_runtime_contexts, monkeypatch):
    global registry_seen
    registry_seen = workflow_runtime_contexts
    contexts_seen[case_id] = id(get_runtime_context())
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: attachments.append((kwargs["name"], str(body))))
    first_attachment = len(attachments)
    if case_id == "DEPENDENT":
        assert Context.get_variable("predecessor_value") == "workflow-canary"
        assert Context.is_case_passed("PREDECESSOR")
        assert Context.get_effect_state("PREDECESSOR") == Context.EFFECT_APPLIED
        assert Context.get_effect("PREDECESSOR")["snapshot"]["before"] == {{"field": "same-workflow"}}
        assert contexts_seen["DEPENDENT"] == contexts_seen["PREDECESSOR"]
    else:
        assert Context.all() == {{"variables": {{}}, "cases": {{}}, "effects": {{}}}}
    {entry_call}
    assert Context.is_case_passed(case_id)
    if case_id == "PREDECESSOR":
        Context.mark_effect(case_id, state=Context.EFFECT_APPLIED,
                            snapshot={{"before": {{"field": "same-workflow"}}}})
    if case_id == "DEPENDENT":
        assert urlsplit(requests_seen[-1]).path == "/consumer/workflow-canary"
        assert any("workflow-canary" in body for name, body in attachments[first_attachment:]
                   if name == "context after effect")
    if case_id == "INDEPENDENT":
        assert contexts_seen[case_id] != contexts_seen["PREDECESSOR"]
        assert Context.all() == {{"variables": {{}}, "cases": {{"INDEPENDENT": {{"passed": True}}}}, "effects": {{}}}}
        for name, body in attachments[first_attachment:]:
            if name == "context after effect":
                assert "PREDECESSOR" not in body and "workflow-canary" not in body
                assert "same-workflow" not in body
'''


@pytest.mark.parametrize("kind", ["generic", "contract"])
def test_real_pytest_items_share_dependency_state_and_isolate_other_groups(tmp_path, kind):
    """Collect three real items with the original fixture override and runner."""
    module_name = "test_03_api_cases" if kind == "generic" else "test_04_user_update_contract"
    entry_call = (
        "entry.TestApiCases().test_api_case(api_client_factory, credential_provider, request, case_id)"
        if kind == "generic" else
        "entry.TestUserUpdateContract().test_update_contract(api_client_factory, credential_provider, request, case_id)"
    )
    report = tmp_path / "workflow-result.json"
    test_file = tmp_path / "test_workflow.py"
    test_file.write_text(_NESTED_WORKFLOW.format(
        module_name=module_name, report_path=str(report), entry_call=entry_call,
    ), encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "from wizbank_api_test.tests.conftest import bind_context, pytest_addoption\n",
        encoding="utf-8",
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    command = [sys.executable, "-m", "pytest", "-c", str(config), str(test_file), "-q"]
    if kind == "contract":
        command += ["--tag", "update_contract"]
    env = os.environ.copy()
    project_parent = Path(__file__).resolve().parents[3]
    # Preserve the import alias when the checkout directory has a different name.
    package_parent = Path(next(iter(wizbank_api_test.__path__))).parent
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(package_parent), str(project_parent), env.get("PYTHONPATH", ""),
    ]))
    completed = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "3 passed" in completed.stdout
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["requests"] == [
        "https://workflow.invalid/producer",
        "https://workflow.invalid/consumer/workflow-canary",
        "https://workflow.invalid/independent",
    ]
    assert result["contexts"]["PREDECESSOR"] == result["contexts"]["DEPENDENT"]
    assert result["contexts"]["PREDECESSOR"] != result["contexts"]["INDEPENDENT"]
    assert result["registry_discarded"] and result["binding_restored"]
