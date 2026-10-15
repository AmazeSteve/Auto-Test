"""A typed Host-be credential never crosses a Runtime host boundary."""

from copy import deepcopy

import allure
import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter

from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.services.credential_provider import Credential
from wizbank_api_test.tests import conftest as fixtures
from wizbank_api_test.tests.unit.credential_helpers import FakeCredentialProvider


ADMIN_COOKIE = "SESSION=HOST_BE_PRIVATE_COOKIE"


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
        raise AssertionError("Real HTTP is forbidden in admin host-boundary tests")

    monkeypatch.setattr(HTTPAdapter, "send", reject_network)
    endpoint_dir = tmp_path / "endpoints"
    endpoint_dir.mkdir()
    monkeypatch.setattr(EndpointRegistry, "_instance", None)
    EndpointRegistry(endpoint_dir)
    yield
    Context.clear()


@pytest.fixture
def evidence(monkeypatch):
    entries = []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: entries.append(str(body)))
    monkeypatch.setattr(allure.dynamic, "parameter", lambda *args, **kwargs: entries.append(repr((args, kwargs))))
    return entries


def case_for(host="Host-be", auth="admin"):
    return {
        "id": "HOST_BOUNDARY", "method": "GET", "path": "/offline-read",
        "host": host, "auth": auth, "headers": {"X-Request": "ordinary"},
        "validate": [{"type": "status_code", "expected": 200}],
    }


def capture_endpoint(monkeypatch, host="Host-be", auth="admin"):
    endpoint = {"method": "GET", "path": "/offline-capture", "auth": auth}
    if host is not None:
        endpoint["host"] = host
    monkeypatch.setattr(EndpointRegistry, "get", lambda self, endpoint_id: deepcopy(endpoint))


def assert_no_credential(*values):
    for value in values:
        assert "HOST_BE_PRIVATE_COOKIE" not in repr(value)


@pytest.mark.parametrize("host", ["Host-226", "Host-156"])
def test_admin_case_rejects_foreign_credential_before_headers_or_transport(monkeypatch, evidence, host):
    client = FakeClient(host)
    selections, header_calls = [], []

    def factory(host_key, *, principal):
        selections.append((host_key, principal))
        return client

    def header_builder(case, credential):
        header_calls.append(case)
        return {"Cookie": credential.cookie}

    monkeypatch.setattr(CaseRunner, "build_header", header_builder)
    case = case_for(host)
    original = deepcopy(case)
    provider = FakeCredentialProvider(
        {(host, "admin"): client},
        credentials={(host, "admin"): Credential("Host-be", "admin", ADMIN_COOKIE)},
    )
    with pytest.raises(ValueError) as raised:
        CaseRunner.run(case, factory, provider)

    assert selections == [(host, "admin")]
    assert header_calls == client.requests == []
    assert evidence == []
    assert Context.snapshot()["size"] == 0
    assert_no_credential(str(raised.value), evidence, Context.all())
    assert case == original


@pytest.mark.parametrize("host", ["Host-226", "Host-156"])
def test_direct_admin_header_builder_rejects_foreign_credential_before_variable_resolution(monkeypatch, host):
    calls = []

    def resolve(value):
        calls.append(value)
        return value

    monkeypatch.setattr(VariableResolver, "resolve", resolve)
    case = case_for(host)
    case["headers"]["Cookie"] = ADMIN_COOKIE
    original = deepcopy(case)
    with pytest.raises(ValueError) as raised:
        CaseRunner.build_header(case, Credential("Host-be", "admin", ADMIN_COOKIE))

    assert calls == []
    assert_no_credential(str(raised.value), Context.all())
    assert case == original


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("custom_headers", [False, True], ids=["runtime-headers", "custom-headers"])
@pytest.mark.parametrize("root_host,endpoint_host,override", [
    pytest.param("Host-be", "Host-226", {}, id="endpoint-226"),
    pytest.param("Host-be", "Host-156", {}, id="endpoint-156"),
    pytest.param("Host-be", "Host-be", {"host": "Host-226"}, id="capture-override"),
    pytest.param("Host-226", None, {}, id="root-host-fallback"),
])
def test_admin_capture_rejects_foreign_credential_for_normalized_host_before_headers_and_transport(
        monkeypatch, evidence, phase, custom_headers, root_host, endpoint_host, override):
    capture_endpoint(monkeypatch, endpoint_host)
    normalized_host = override.get("host", endpoint_host or root_host)
    client = FakeClient(normalized_host)
    selections, header_calls = [], []

    def factory(host_key, *, principal):
        selections.append((host_key, principal))
        return client

    def custom_header_builder(case, credential):
        header_calls.append(deepcopy(case))
        return {"Cookie": credential.cookie}

    header_builder = custom_header_builder if custom_headers else CaseRunner.build_header
    case = case_for(root_host)
    config = {"endpoint": "OFFLINE_CAPTURE", **override}
    original = deepcopy((case, config))
    provider = FakeCredentialProvider(
        {(normalized_host, "admin"): client},
        credentials={(normalized_host, "admin"): Credential("Host-be", "admin", ADMIN_COOKIE)},
    )
    with pytest.raises(ValueError) as raised:
        EffectExecutor.execute_capture(case, config, phase, factory, provider, header_builder)

    assert selections == [(normalized_host, "admin")]
    assert header_calls == client.requests == []
    assert evidence == []
    assert Context.snapshot()["size"] == 0
    assert_no_credential(str(raised.value), evidence, Context.all())
    assert (case, config) == original


@pytest.mark.parametrize("phase", ["before", "after"])
def test_foreign_anonymous_root_can_capture_host_be_admin_using_capture_identity(monkeypatch, evidence, phase):
    capture_endpoint(monkeypatch, "Host-be", "admin")
    client = FakeClient()
    selections = []

    def factory(host_key, *, principal):
        selections.append((host_key, principal))
        return client

    case = case_for("Host-226", "none")
    config = {"endpoint": "OFFLINE_CAPTURE"}
    original = deepcopy((case, config))
    provider = FakeCredentialProvider({("Host-be", "admin"): client}, cookie=ADMIN_COOKIE)
    result = EffectExecutor.execute_capture(case, config, phase, factory, provider, CaseRunner.build_header)

    assert selections == [("Host-be", "admin")]
    assert result == FakeResponse().json()
    assert client.requests[0]["headers"]["Cookie"] == ADMIN_COOKIE
    assert_no_credential(evidence, Context.all())
    assert (case, config) == original


@pytest.mark.parametrize("phase", ["before", "after"])
def test_host_be_admin_root_can_capture_foreign_anonymous_host(monkeypatch, evidence, phase):
    capture_endpoint(monkeypatch, "Host-226", "none")
    client = FakeClient()
    selections = []

    def factory(host_key, *, principal):
        selections.append((host_key, principal))
        return client

    EffectExecutor.execute_capture(case_for(), {"endpoint": "OFFLINE_CAPTURE"}, phase,
                                   factory, None, CaseRunner.build_header)

    assert selections == [("Host-226", "none")]
    assert client.requests[0]["headers"] == {}
    assert_no_credential(evidence, Context.all())


@pytest.mark.parametrize("host", ["Host-226", "Host-156"])
def test_anonymous_case_remains_supported_on_other_hosts(evidence, host):
    client = FakeClient()
    selections = []

    def factory(host_key, *, principal):
        selections.append((host_key, principal))
        return client

    CaseRunner.run(case_for(host, "none"), factory, None, apply_metadata=False)

    assert selections == [(host, "none")]
    assert len(client.requests) == 1
    assert client.requests[0]["headers"] == {"X-Request": "ordinary"}
    assert_no_credential(evidence, Context.all())


class FakeConfig:
    def get_base_url(self, host_key):
        return {"Host-be": "https://be.example.invalid",
                "Host-226": "https://other.example.invalid"}[host_key]

    def get_default_headers(self):
        return {"X-Offline": "true"}

    def get_timeout(self):
        return 7

    def get_verify_ssl(self):
        return True


class RecordingAdapter(BaseAdapter):
    """Real Requests preparation, with transport confined to this fake adapter."""

    def __init__(self):
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        response._content = b'{"code":200}'
        response.encoding = "utf-8"
        return response

    def close(self):
        pass


def test_factory_keeps_cross_host_admin_sessions_and_rejects_host_be_credential_on_foreign_request(evidence):
    factory = fixtures.api_client_factory.__wrapped__(FakeConfig())
    admin = factory("Host-be", "admin")
    foreign_admin = factory("Host-226", "admin")
    assert admin is not foreign_admin
    assert admin.session is not foreign_admin.session
    assert factory("Host-226", "admin") is foreign_admin
    admin.session.cookies.set("SESSION", "HOST_BE_PRIVATE_COOKIE", domain="be.example.invalid", path="/")
    initial_cookies = deepcopy(admin.session.cookies)
    admin_adapter, foreign_adapter = RecordingAdapter(), RecordingAdapter()
    admin.session.mount("https://", admin_adapter)
    foreign_admin.session.mount("https://", foreign_adapter)

    provider = FakeCredentialProvider(
        {("Host-be", "admin"): admin, ("Host-226", "admin"): foreign_admin},
        credentials={
            ("Host-be", "admin"): Credential("Host-be", "admin", ADMIN_COOKIE),
            ("Host-226", "admin"): Credential("Host-be", "admin", ADMIN_COOKIE),
        },
    )
    CaseRunner.run(case_for(), factory, provider, apply_metadata=False)
    assert len(admin_adapter.requests) == 1
    prepared = admin_adapter.requests[0]
    assert prepared.url == "https://be.example.invalid/offline-read"
    assert prepared.headers["Cookie"] == ADMIN_COOKIE

    with pytest.raises(ValueError) as raised:
        CaseRunner.run(case_for("Host-226"), factory, provider, apply_metadata=False)

    assert foreign_adapter.requests == []
    assert not foreign_admin.session.cookies
    assert admin.session.cookies == initial_cookies
    foreign_prepared = foreign_admin.session.prepare_request(
        requests.Request("GET", foreign_admin.base_url + "/offline-read")
    )
    assert "Cookie" not in foreign_prepared.headers
    assert "Authorization" not in foreign_prepared.headers
    assert_no_credential(str(raised.value), evidence, Context.all())
