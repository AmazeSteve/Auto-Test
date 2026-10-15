"""Real runtime entrypoints with lazy credentials and fake Session transport."""

from copy import deepcopy
import inspect
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import allure
import pytest
import requests

from wizbank_api_test.core.client import ApiClient
from wizbank_api_test.core.config import Config
from wizbank_api_test.core.context import Context
from wizbank_api_test.services.credential_provider import CredentialProvider
from wizbank_api_test.services import user_update_acceptance as harness
from wizbank_api_test.tests import conftest as fixtures
from wizbank_api_test.tests import test_01_login as login_entry
from wizbank_api_test.tests import test_03_api_cases as yaml_entry
from wizbank_api_test.tests import test_04_user_update_contract as contract_entry
from wizbank_api_test.tests import test_05_user_update_preflight as preflight_entry
from wizbank_api_test.tests import test_06_user_update_lifecycle_acceptance as acceptance_entry


class FakeResponse:
    status_code = 200

    def __init__(self, *, cookie=None):
        self.headers = {} if cookie is None else {"Set-Cookie": cookie}
        self.text = '{"code": 200}'

    def json(self):
        return {"code": 200}


class ObservedProvider(CredentialProvider):
    def __init__(self, factory, config):
        super().__init__(factory, config)
        self.lookups = []

    def get(self, host_key, principal):
        self.lookups.append((host_key, principal))
        return super().get(host_key, principal)


@pytest.fixture
def offline_auth(monkeypatch, bind_context):
    Context.clear()

    def reject_network(*args, **kwargs):
        pytest.fail("Credential entrypoint tests must not use real HTTP")

    monkeypatch.setattr(requests.Session, "request", reject_network)
    config = Config.__new__(Config)
    config.data = {
        "users": {
            "admin": {"username": "offline-admin", "password": "offline-admin-password"},
            "learner": {"username": "offline-learner", "password": "offline-learner-password"},
        },
    }
    clients, calls = {}, []

    def factory(host_key="Host-be", principal="none"):
        key = (host_key, principal)
        if principal != "none" and key in clients:
            return clients[key]
        client = ApiClient("https://example.invalid", host_key=host_key, principal=principal)

        def transport(**kwargs):
            calls.append((key, deepcopy(kwargs)))
            if urlsplit(kwargs["url"]).path == "/app/user/login/v3":
                client.session.cookies.set("SESSION", f"offline-{principal}")
                return FakeResponse(cookie=f"SESSION=offline-{principal}")
            return FakeResponse()

        monkeypatch.setattr(client.session, "request", transport)
        if principal != "none":
            clients[key] = client
        return client

    provider = ObservedProvider(factory, config)
    assert clients == {} and calls == [] and provider.lookups == []
    yield SimpleNamespace(factory=factory, config=config, provider=provider, calls=calls, clients=clients)
    Context.clear()


def request_stub():
    return SimpleNamespace(config=SimpleNamespace(getoption=lambda name: None))


def login_calls(auth, principal):
    return [kwargs for (_, p), kwargs in auth.calls
            if p == principal and urlsplit(kwargs["url"]).path == "/app/user/login/v3"]


@pytest.mark.parametrize("principal", ["none", None])
def test_generic_yaml_anonymous_does_not_lookup_credentials_or_login(monkeypatch, offline_auth, principal):
    case = {"id": "OFFLINE_ANONYMOUS", "name": "anonymous", "method": "GET", "path": "/ready", "auth": principal}
    monkeypatch.setattr(yaml_entry, "CASE_MAP", {case["id"]: case})
    monkeypatch.setattr(yaml_entry, "ALL_CASES", [case])
    monkeypatch.setattr(yaml_entry, "LIFECYCLE_CHILD_MAP", {})
    original = deepcopy(case)

    yaml_entry.TestApiCases().test_api_case(offline_auth.factory, offline_auth.provider, request_stub(), case["id"])

    assert offline_auth.provider.lookups == []
    assert login_calls(offline_auth, "admin") == login_calls(offline_auth, "learner") == []
    assert len(offline_auth.calls) == 1 and offline_auth.calls[0][0] == ("Host-be", "none")
    assert "Cookie" not in offline_auth.calls[0][1]["headers"]
    assert Context.is_case_passed(case["id"])
    assert case == original


def test_generic_yaml_learner_only_does_not_admin_login(monkeypatch, offline_auth):
    case = {"id": "OFFLINE_LEARNER", "name": "learner", "method": "GET", "path": "/ready", "auth": "learner"}
    monkeypatch.setattr(yaml_entry, "CASE_MAP", {case["id"]: case})
    monkeypatch.setattr(yaml_entry, "ALL_CASES", [case])
    monkeypatch.setattr(yaml_entry, "LIFECYCLE_CHILD_MAP", {})

    yaml_entry.TestApiCases().test_api_case(offline_auth.factory, offline_auth.provider, request_stub(), case["id"])

    assert offline_auth.provider.lookups == [("Host-be", "learner")]
    assert login_calls(offline_auth, "admin") == []
    assert len(login_calls(offline_auth, "learner")) == 1
    assert ("Host-be", "admin") not in offline_auth.clients
    assert offline_auth.calls[-1][1]["headers"]["Cookie"] == "SESSION=offline-learner"
    assert Context.is_case_passed(case["id"])


@pytest.mark.parametrize("entry", [yaml_entry.TestApiCases.test_api_case, contract_entry.TestUserUpdateContract.test_update_contract])
def test_generic_entrypoint_fixture_dependencies_do_not_include_eager_admin_cookie(entry):
    parameters = inspect.signature(entry).parameters
    assert "admin_cookie" not in parameters
    assert "credential_provider" in parameters and "api_client_factory" in parameters


def test_contract_entrypoint_retains_tag_gate_before_runtime(monkeypatch, offline_auth):
    monkeypatch.setattr(contract_entry.LifecycleRunner, "run_case", lambda *args, **kwargs: pytest.fail("contract gate must run first"))
    with pytest.raises(pytest.skip.Exception, match="update_contract"):
        contract_entry.TestUserUpdateContract().test_update_contract(
            offline_auth.factory, offline_auth.provider, request_stub(), "NOT_ACCESSED",
        )
    assert offline_auth.provider.lookups == [] and offline_auth.calls == []


@pytest.mark.parametrize("kind", ["preflight", "acceptance"])
def test_c1_requests_lazy_provider_after_gate_and_login_inside_safe_evidence(monkeypatch, offline_auth, kind):
    entry = preflight_entry if kind == "preflight" else acceptance_entry
    function = entry.test_user_update_preflight if kind == "preflight" else entry.test_user_update_lifecycle_acceptance
    service_name = "preflight" if kind == "preflight" else "accept_lifecycle"
    gate_name = "require_username" if kind == "preflight" else "require_real_gates"
    evidence, events = [], []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append((kwargs["name"], body)))
    monkeypatch.setattr(entry, "load_cases", lambda: {harness.ROOT_CASE_ID: {"id": harness.ROOT_CASE_ID}})

    def gate(*args):
        events.append("gate")
        return "offline-explicit-target"

    def fixture(name):
        assert events and events[0] == "gate"
        assert offline_auth.provider.lookups == [] and offline_auth.calls == []
        events.append(name)
        return {"api_client_factory": offline_auth.factory, "credential_provider": offline_auth.provider}[name]

    def service(root, cases, factory, provider, request, *args):
        assert factory is offline_auth.factory and provider is offline_auth.provider
        events.append("service")
        credential = provider.get("Host-be", "admin")
        assert credential.cookie == "SESSION=offline-admin"
        name = "C1 preflight summary" if kind == "preflight" else "C1 lifecycle summary"
        harness._attach(name, {"READY": "YES"})

    monkeypatch.setattr(entry, gate_name, gate)
    monkeypatch.setattr(entry, service_name, service)
    request = request_stub()
    request.getfixturevalue = fixture
    function(request, monkeypatch)

    assert events == ["gate", "api_client_factory", "credential_provider", "service"]
    assert offline_auth.provider.lookups == [("Host-be", "admin")]
    assert len(login_calls(offline_auth, "admin")) == 1
    assert login_calls(offline_auth, "learner") == []
    assert evidence == [("C1 preflight summary" if kind == "preflight" else "C1 lifecycle summary", '{"READY": "YES"}')]
    for secret in ("SESSION=offline-admin", "offline-admin-password", "offline-admin"):
        assert secret not in json.dumps(evidence)


def test_legacy_login_entrypoint_uses_provider_backed_admin_cookie_adapter(offline_auth):
    cookie = fixtures.admin_cookie.__wrapped__(offline_auth.provider)
    login_entry.TestLogin().test_admin_login(cookie)
    assert cookie == "SESSION=offline-admin"
    assert offline_auth.provider.lookups == [("Host-be", "admin")]
    assert len(login_calls(offline_auth, "admin")) == 1
    assert login_calls(offline_auth, "learner") == []
