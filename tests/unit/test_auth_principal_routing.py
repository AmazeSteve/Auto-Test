"""Offline auth routing and fail-closed transport selection for v2.5B-2A."""

from copy import deepcopy
from types import SimpleNamespace

import allure
import pytest
import requests

from wizbank_api_test.core import client as client_module
from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.services.credential_provider import Credential
from wizbank_api_test.tests import conftest as shared_fixtures
from wizbank_api_test.tests.unit.credential_helpers import FakeCredentialProvider


ADMIN_COOKIE = "SESSION=PRIVATE_ADMIN_COOKIE"
PRIVATE_AUTH = "Bearer PRIVATE_AUTHORIZATION"
PRIVATE_PASSWORD = "PRIVATE_PASSWORD"
PRIVATE_TOKEN = "PRIVATE_TOKEN"
MISSING = object()


class FakeResponse:
    status_code = 200
    headers = {}
    text = '{"code":200,"data":{"value":"captured"}}'

    def json(self):
        return {"code": 200, "data": {"value": "captured"}}


class FakeClient:
    def __init__(self, host_key="Host-be", principal="admin"):
        self.requests = []
        self.host_key = host_key
        self.principal = principal
        self.session = object()

    def request(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return FakeResponse()


@pytest.fixture(autouse=True)
def offline(monkeypatch, bind_context, tmp_path):
    Context.clear()

    def reject_network(*args, **kwargs):
        raise AssertionError("auth routing tests must not contact a real host")

    monkeypatch.setattr(requests.Session, "request", reject_network)
    endpoint_dir = tmp_path / "endpoints"
    endpoint_dir.mkdir()
    monkeypatch.setattr(EndpointRegistry, "_instance", None)
    EndpointRegistry(endpoint_dir)
    yield
    Context.clear()


def case_for(auth=MISSING, host="Host-be"):
    case = {
        "id": "ROUTING", "method": "GET", "path": "/offline-routing",
        "host": host, "headers": {"X-Request": "ordinary"},
        "validate": [{"type": "status_code", "expected": 200}],
    }
    if auth is not MISSING:
        case["auth"] = auth
    return case


def capture_endpoint(monkeypatch, auth="admin", host="Host-226"):
    endpoint = {"method": "GET", "path": "/offline-capture", "host": host, "auth": auth}
    monkeypatch.setattr(EndpointRegistry, "get", lambda self, endpoint_id: deepcopy(endpoint))
    return endpoint


def assert_no_credentials(value):
    text = repr(value)
    for secret in (ADMIN_COOKIE, PRIVATE_AUTH, PRIVATE_PASSWORD, PRIVATE_TOKEN):
        assert secret not in text


@pytest.mark.parametrize("host", ["Host-be", "Host-226"])
@pytest.mark.parametrize("auth,principal", [
    pytest.param("admin", "admin", id="admin"),
    pytest.param("none", "none", id="none"),
    pytest.param(None, "none", id="null"),
    pytest.param(MISSING, "none", id="omitted"),
])
def test_case_runner_routes_host_and_principal_without_storing_credentials(host, auth, principal):
    client = FakeClient(host, principal)
    selections = []

    def factory(host_key, *, principal):
        selections.append((host_key, principal))
        return client

    case = case_for(auth, host)
    original = deepcopy(case)
    provider = FakeCredentialProvider(
        {(host, principal): client} if principal != "none" else {}, cookie=ADMIN_COOKIE,
    )
    CaseRunner.run(case, factory, provider, apply_metadata=False)

    assert selections == [(host, principal)]
    assert len(client.requests) == 1
    headers = client.requests[0]["headers"]
    assert headers["X-Request"] == "ordinary"
    if principal == "admin":
        assert headers["Cookie"] == ADMIN_COOKIE
    else:
        assert not {str(key).lower() for key in headers} & {"cookie", "authorization"}
        assert provider.get_calls == provider.match_calls == []
    assert Context.is_case_passed(case["id"])
    assert Context.all()["variables"] == {}
    assert_no_credentials(Context.snapshot())
    assert case == original


@pytest.mark.parametrize("auth", ["none", None, MISSING], ids=["none", "null", "omitted"])
@pytest.mark.parametrize("header", ["Cookie", "cOoKiE", "Authorization", "aUtHoRiZaTiOn"])
def test_anonymous_auth_headers_fail_before_transport_and_before_resolving_secrets(monkeypatch, auth, header):
    client = FakeClient()
    case = case_for(auth)
    case["headers"][header] = "{{PRIVATE_PASSWORD_PRIVATE_TOKEN}}"
    original = deepcopy(case)
    evidence = []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append(str(body)))

    with pytest.raises(ValueError) as raised:
        CaseRunner.run(case, lambda host, principal="none": client, None, apply_metadata=False)

    assert "anonymous" in str(raised.value).lower()
    assert client.requests == []
    assert_no_credentials(str(raised.value))
    assert "PRIVATE_PASSWORD_PRIVATE_TOKEN" not in str(raised.value)
    assert_no_credentials(evidence)
    assert Context.snapshot()["size"] == 0
    assert case == original


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("endpoint_auth,root_auth,principal", [
    ("admin", "none", "admin"),
    ("none", "admin", "none"),
    (None, "admin", "none"),
])
def test_effect_capture_routes_by_normalized_capture_auth_not_root(monkeypatch, phase, endpoint_auth, root_auth, principal):
    host = "Host-be" if principal == "admin" else "Host-226"
    capture_endpoint(monkeypatch, endpoint_auth, host)
    client = FakeClient(host, principal)
    selections = []

    def factory(host_key, *, principal):
        selections.append((host_key, principal))
        return client

    root = case_for(root_auth)
    root["effect"] = {"capture": {
        "enabled": True,
        "before": {"endpoint": "OFFLINE_CAPTURE"},
        "after": {"endpoint": "OFFLINE_CAPTURE"},
    }}
    original = deepcopy(root)
    capture = getattr(EffectExecutor, "capture_" + phase)
    provider = FakeCredentialProvider(
        {(host, principal): client} if principal != "none" else {}, cookie=ADMIN_COOKIE,
    )
    result = capture(root, factory, provider, CaseRunner.build_header)

    assert selections == [(host, principal)]
    assert len(client.requests) == 1
    assert result == FakeResponse().json()
    if principal == "admin":
        assert client.requests[0]["headers"]["Cookie"] == ADMIN_COOKIE
    else:
        assert client.requests[0]["headers"] == {}
        assert provider.get_calls == provider.match_calls == []
    assert Context.snapshot()["size"] == 0
    assert_no_credentials(Context.all())
    assert root == original


@pytest.mark.parametrize("override,principal", [("none", "none"), (None, "none"), ("admin", "admin")])
def test_explicit_capture_auth_and_host_override_control_routing(monkeypatch, override, principal):
    capture_endpoint(monkeypatch, "admin", "Host-be")
    client = FakeClient("Host-226", principal)
    selections = []

    def factory(host_key, *, principal):
        selections.append((host_key, principal))
        return client

    config = {"endpoint": "OFFLINE_CAPTURE", "auth": override, "host": "Host-226"}
    provider = FakeCredentialProvider(
        {("Host-226", principal): client} if principal != "none" else {}, cookie=ADMIN_COOKIE,
    )
    EffectExecutor.execute_capture(case_for("admin"), config, "before", factory, provider, CaseRunner.build_header)

    assert selections == [("Host-226", principal)]
    assert len(client.requests) == 1
    assert_no_credentials(Context.all())


def test_unconfigured_learner_case_fails_closed_before_transport(monkeypatch):
    selections = []
    evidence = []
    client = FakeClient(principal="learner")

    def factory(host, *, principal):
        selections.append((host, principal))
        return client

    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append(str(body)))
    case = case_for("learner")
    case["headers"] = {"Cookie": ADMIN_COOKIE, "Authorization": PRIVATE_AUTH}
    provider = FakeCredentialProvider({("Host-be", "learner"): client}, credentials={})
    with pytest.raises(ValueError) as raised:
        CaseRunner.run(case, factory, provider, apply_metadata=False)

    assert selections == [("Host-be", "learner")]
    assert provider.get_calls == [("Host-be", "learner")]
    assert client.requests == []
    assert_no_credentials(str(raised.value))
    assert_no_credentials(evidence)
    assert Context.snapshot()["size"] == 0


def test_learner_header_builder_rejects_admin_credential_with_safe_message():
    case = case_for("learner")
    case["headers"] = {"Cookie": ADMIN_COOKIE, "Authorization": PRIVATE_AUTH}
    with pytest.raises(ValueError) as raised:
        CaseRunner.build_header(case, Credential("Host-be", "admin", ADMIN_COOKIE))
    assert_no_credentials(str(raised.value))
    assert Context.snapshot()["size"] == 0


@pytest.mark.parametrize("phase", ["before", "after"])
def test_unconfigured_learner_capture_fails_closed_even_with_custom_header_builder(monkeypatch, phase):
    capture_endpoint(monkeypatch, "learner")
    client = FakeClient("Host-226", "learner")
    selections, header_calls = [], []

    def factory(host, *, principal):
        selections.append((host, principal))
        return client

    def header_builder(case, credential):
        header_calls.append(case)
        return {}

    provider = FakeCredentialProvider({("Host-226", "learner"): client}, credentials={})
    with pytest.raises(ValueError):
        EffectExecutor.execute_capture(case_for("admin"), {"endpoint": "OFFLINE_CAPTURE"}, phase,
                                       factory, provider, header_builder)

    assert selections == [("Host-226", "learner")]
    assert provider.get_calls == [("Host-226", "learner")]
    assert header_calls == client.requests == []
    assert Context.snapshot()["size"] == 0


@pytest.mark.parametrize("header", ["cOoKiE", "aUtHoRiZaTiOn"])
def test_anonymous_capture_rejects_auth_header_before_transport(monkeypatch, header):
    capture_endpoint(monkeypatch, "none")
    client = FakeClient()
    config = {"endpoint": "OFFLINE_CAPTURE", "headers": {header: PRIVATE_AUTH}}
    with pytest.raises(ValueError) as raised:
        EffectExecutor.execute_capture(case_for("admin"), config, "before",
                                       lambda host, principal="none": client, None, CaseRunner.build_header)
    assert client.requests == []
    assert_no_credentials(str(raised.value))
    assert Context.snapshot()["size"] == 0


@pytest.mark.parametrize("header", ["cOoKiE", "aUtHoRiZaTiOn"])
def test_anonymous_capture_rejects_auth_from_custom_header_builder(monkeypatch, header):
    capture_endpoint(monkeypatch, "none")
    client = FakeClient()
    header_calls = []

    def header_builder(case, credential):
        header_calls.append(case)
        return {header: PRIVATE_AUTH}

    with pytest.raises(ValueError) as raised:
        EffectExecutor.execute_capture(case_for("admin"), {"endpoint": "OFFLINE_CAPTURE"}, "before",
                                       lambda host, principal="none": client, None, header_builder)

    assert len(header_calls) == 1
    assert client.requests == []
    assert_no_credentials(str(raised.value))
    assert Context.snapshot()["size"] == 0


def test_anonymous_legacy_one_argument_factory_remains_compatible():
    client = FakeClient()
    selections = []

    def factory(host):
        selections.append(host)
        return client

    CaseRunner.run(case_for("none"), factory, None, apply_metadata=False)
    assert selections == ["Host-be"]
    assert len(client.requests) == 1
    assert "Cookie" not in client.requests[0]["headers"]


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_authenticated_principal_never_falls_back_to_host_only_factory(principal):
    selections = []

    def factory(host):
        selections.append(host)
        return FakeClient()

    with pytest.raises(TypeError):
        client_module.get_principal_client(factory, "Host-be", principal)
    assert selections == []


def test_none_factory_with_other_optional_arguments_does_not_use_legacy_fallback():
    selections = []

    def factory(host, unrelated_option=None):
        selections.append(host)
        return FakeClient()

    with pytest.raises(TypeError):
        client_module.get_principal_client(factory, "Host-be", "none")
    assert selections == []


@pytest.mark.parametrize("legacy", [False, True], ids=["principal-aware", "legacy-none"])
def test_factory_internal_typeerror_is_propagated_without_retry(legacy):
    error = TypeError("offline factory body failure")
    calls = []

    if legacy:
        def factory(host):
            calls.append((host, "none"))
            raise error
    else:
        def factory(host, *, principal):
            calls.append((host, principal))
            raise error

    with pytest.raises(TypeError) as raised:
        client_module.get_principal_client(factory, "Host-be", "none")
    assert raised.value is error
    assert calls == [("Host-be", "none")]


def test_uninspectable_factory_uses_explicit_principal_without_legacy_retry():
    calls = []
    error = TypeError("offline uninspectable factory failure")

    class Factory:
        __signature__ = "unavailable"

        def __call__(self, host, *, principal):
            calls.append((host, principal))
            raise error

    with pytest.raises(TypeError) as raised:
        client_module.get_principal_client(Factory(), "Host-be", "none")
    assert raised.value is error
    assert calls == [("Host-be", "none")]


def test_variadic_factory_is_not_treated_as_a_legacy_host_only_factory():
    calls = []

    def factory(*args):
        calls.append(args)
        return FakeClient()

    with pytest.raises(TypeError):
        client_module.get_principal_client(factory, "Host-be", "none")
    assert calls == []


@pytest.mark.parametrize("invalid", ["PRIVATE_TOKEN", "", "ADMIN", 1, []])
def test_invalid_principal_is_rejected_without_echoing_credentials(invalid):
    client = FakeClient()
    with pytest.raises(ValueError) as raised:
        CaseRunner.run(case_for(invalid), lambda host, principal="none": client, None, apply_metadata=False)
    assert client.requests == []
    assert_no_credentials(str(raised.value))
    assert Context.snapshot()["size"] == 0


def test_actual_factory_and_case_runner_keep_admin_ownership_and_anonymous_requests_separate(monkeypatch):
    config = SimpleNamespace(
        get_base_url=lambda host: "https://example.invalid",
        get_default_headers=lambda: {"X-Default": "ordinary"},
        get_timeout=lambda: 30,
        get_verify_ssl=lambda: True,
    )
    base_factory = shared_fixtures.api_client_factory.__wrapped__(config)
    selections, transport = [], []

    def request(session, **kwargs):
        transport.append((session, deepcopy(kwargs)))
        return FakeResponse()

    def factory(host, *, principal):
        client = base_factory(host, principal=principal)
        selections.append((host, principal, client))
        return client

    monkeypatch.setattr(requests.Session, "request", request)
    provider = FakeCredentialProvider({("Host-be", "admin"): base_factory("Host-be", "admin")}, cookie=ADMIN_COOKIE)
    for index, auth in enumerate(("admin", "none", "admin", None)):
        case = case_for(auth)
        case["id"] = "REAL_FACTORY_" + str(index)
        CaseRunner.run(case, factory, provider, apply_metadata=False)

    assert [(host, principal) for host, principal, _ in selections] == [
        ("Host-be", "admin"), ("Host-be", "none"), ("Host-be", "admin"), ("Host-be", "none"),
    ]
    assert selections[0][2] is selections[2][2]
    assert selections[1][2] is not selections[3][2]
    assert len({id(session) for session, _ in transport}) == 3
    assert transport[0][1]["headers"]["Cookie"] == ADMIN_COOKIE
    assert transport[2][1]["headers"]["Cookie"] == ADMIN_COOKIE
    for index in (1, 3):
        assert not {str(key).lower() for key in transport[index][1]["headers"]} & {"cookie", "authorization"}
    assert Context.all()["variables"] == {}
    assert_no_credentials(Context.snapshot())


def test_admin_request_evidence_redacts_credentials_without_writing_context(monkeypatch):
    evidence = []
    transports = []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append(str(body)))
    client = client_module.ApiClient("https://example.invalid", principal="admin", host_key="Host-be")

    def request(**kwargs):
        transports.append(deepcopy(kwargs))
        return FakeResponse()

    monkeypatch.setattr(client.session, "request", request)
    case = case_for("admin")
    case["headers"]["Authorization"] = PRIVATE_AUTH
    case["json"] = {"password": PRIVATE_PASSWORD, "token": PRIVATE_TOKEN}
    provider = FakeCredentialProvider({("Host-be", "admin"): client}, cookie=ADMIN_COOKIE)
    CaseRunner.run(case, lambda host, principal="none": client, provider, apply_metadata=False)

    assert transports[0]["headers"]["Cookie"] == ADMIN_COOKIE
    assert transports[0]["headers"]["Authorization"] == PRIVATE_AUTH
    assert transports[0]["json"] == case["json"]
    assert_no_credentials(evidence)
    assert_no_credentials(Context.snapshot())


@pytest.mark.parametrize("region", ["headers", "default_headers"])
@pytest.mark.parametrize("header", ["cOoKiE", "aUtHoRiZaTiOn"])
def test_anonymous_client_rejects_auth_headers_before_variable_resolution(monkeypatch, region, header):
    client = client_module.ApiClient("https://example.invalid", principal="none")
    unsafe = {header: "{{PRIVATE_PASSWORD_PRIVATE_TOKEN}}"}
    evidence, transports = [], []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append(str(body)))
    monkeypatch.setattr(client.session, "request", lambda **kwargs: transports.append(kwargs))

    def unexpected_resolution(value):
        raise AssertionError("Anonymous auth headers must be rejected before variable resolution")

    monkeypatch.setattr(client_module.VariableResolver, "resolve", unexpected_resolution)
    arguments = {}
    if region == "default_headers":
        client.default_headers.update(unsafe)
    else:
        arguments["headers"] = unsafe
    with pytest.raises(ValueError) as raised:
        client.get("/offline-routing", **arguments)
    assert "anonymous" in str(raised.value).lower()
    assert "PRIVATE_PASSWORD_PRIVATE_TOKEN" not in str(raised.value)
    assert transports == evidence == []
    assert unsafe == {header: "{{PRIVATE_PASSWORD_PRIVATE_TOKEN}}"}
    if region == "default_headers":
        assert client.default_headers == unsafe
    assert_no_credentials(Context.snapshot())
