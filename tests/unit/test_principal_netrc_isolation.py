"""Use real Requests preparation and redirects with synthetic NETRC, offline."""

import base64
from collections import deque
from copy import deepcopy
from email.message import Message
from types import SimpleNamespace

import allure
import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from requests.structures import CaseInsensitiveDict

from wizbank_api_test.core.client import ApiClient
from wizbank_api_test.core.context import Context
from wizbank_api_test.tests.conftest import api_client_factory


INITIAL_HOST = "initial.example.invalid"
REDIRECT_HOST = "redirect.example.invalid"
NETRC_USER = "SYNTHETIC_NETRC_USERNAME"
NETRC_PASSWORD = "SYNTHETIC_NETRC_PASSWORD"
NETRC_ENCODED = base64.b64encode(
    f"{NETRC_USER}:{NETRC_PASSWORD}".encode("ascii")
).decode("ascii")
EXPLICIT_AUTHORIZATION = "Bearer SYNTHETIC_EXPLICIT_AUTHORIZATION"


class _Config:
    def get_base_url(self, host_key):
        assert host_key == "Host-be"
        return f"https://{INITIAL_HOST}"

    def get_default_headers(self):
        return {"Accept": "application/json"}

    def get_timeout(self):
        return 1

    def get_verify_ssl(self):
        return True


class _OfflineAdapter(BaseAdapter):
    """Keep Requests' preparation, environment merge and redirect machinery."""

    def __init__(self, responses, error=None):
        super().__init__()
        self.responses = deque(responses)
        self.error = error
        self.requests = []
        self.settings = []

    def send(self, request, **kwargs):
        assert isinstance(request, requests.PreparedRequest)
        self.requests.append(request)
        self.settings.append(deepcopy(kwargs))
        if self.error:
            raise self.error
        assert self.responses, "Unexpected request reached offline transport"
        status, headers = self.responses.popleft()
        message = Message()
        for name, value in headers.items():
            message.add_header(name, value)
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict(headers)
        response.url = request.url
        response.request = request
        response._content = b'{"code":200}'
        response._content_consumed = True
        response.encoding = "utf-8"
        response.raw = SimpleNamespace(
            _original_response=SimpleNamespace(msg=message),
            close=lambda: None,
            release_conn=lambda: None,
        )
        return response

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _offline_netrc_environment(tmp_path, monkeypatch, bind_context):
    netrc_file = tmp_path / "synthetic.netrc"
    netrc_file.write_text(
        "".join(
            f"machine {host} login {NETRC_USER} password {NETRC_PASSWORD}\n"
            for host in (INITIAL_HOST, REDIRECT_HOST)
        ),
        encoding="ascii",
    )
    netrc_file.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc_file))
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)

    def deny_real_transport(*args, **kwargs):
        raise AssertionError("Real HTTP transport is forbidden in NETRC tests")

    monkeypatch.setattr(HTTPAdapter, "send", deny_real_transport)
    evidence = []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append(str(body)))
    Context.clear()
    Context.set_variable("ordinary", "unchanged")
    original_context = deepcopy(Context.all())
    yield evidence
    assert Context.all() == original_context
    _assert_no_netrc_secrets(evidence)
    _assert_no_netrc_secrets(Context.all())
    Context.clear()


def _assert_no_netrc_secrets(value):
    rendered = repr(value)
    for secret in (NETRC_USER, NETRC_PASSWORD, NETRC_ENCODED):
        assert secret not in rendered


def _client(principal, construction="direct", default_headers=None):
    if construction == "factory":
        return api_client_factory.__wrapped__(_Config())("Host-be", principal)
    return ApiClient(
        f"https://{INITIAL_HOST}",
        default_headers=default_headers,
        verify_ssl=True,
        principal=principal,
    )


def _mount(client, responses=None, error=None):
    adapter = _OfflineAdapter(responses or [(200, {})], error)
    client.session.mount("https://", adapter)
    return adapter


@pytest.mark.parametrize("principal", ["none", "admin", "learner"])
@pytest.mark.parametrize("construction", ["direct", "factory"])
def test_managed_principal_does_not_prepare_ambient_netrc_auth(principal, construction):
    client = _client(principal, construction)
    adapter = _mount(client)

    response = client.get("/initial")

    assert response.status_code == 200
    assert client.session.trust_env is True
    assert len(adapter.requests) == 1
    assert "Authorization" not in adapter.requests[0].headers
    _assert_no_netrc_secrets(dict(adapter.requests[0].headers))


@pytest.mark.parametrize("principal", ["admin", "learner"])
@pytest.mark.parametrize("source", ["request", "defaults"])
def test_authenticated_explicit_authorization_is_not_overwritten_by_netrc(principal, source):
    headers = {"Authorization": EXPLICIT_AUTHORIZATION}
    client = _client(principal, default_headers=headers if source == "defaults" else None)
    adapter = _mount(client)

    response = client.get("/explicit", headers=headers if source == "request" else None)

    assert response.status_code == 200
    assert adapter.requests[0].headers["Authorization"] == EXPLICIT_AUTHORIZATION
    _assert_no_netrc_secrets(dict(adapter.requests[0].headers))


@pytest.mark.parametrize("principal", ["none", "admin", "learner"])
@pytest.mark.parametrize("destination", ["/redirected", f"https://{REDIRECT_HOST}/redirected"])
def test_redirect_does_not_reintroduce_netrc_auth(principal, destination):
    client = _client(principal, "factory")
    adapter = _mount(client, [(302, {"Location": destination}), (200, {})])

    response = client.get("/initial")

    assert response.status_code == 200
    assert [item.status_code for item in response.history] == [302]
    assert len(adapter.requests) == 2
    assert all("Authorization" not in item.headers for item in adapter.requests)
    for prepared in adapter.requests:
        _assert_no_netrc_secrets(dict(prepared.headers))


@pytest.mark.parametrize("destination,expected", [
    ("/redirected", EXPLICIT_AUTHORIZATION),
    (f"https://{REDIRECT_HOST}/redirected", None),
])
def test_admin_redirect_keeps_same_origin_explicit_auth_and_strips_cross_origin(destination, expected):
    client = _client("admin", "factory")
    adapter = _mount(client, [(302, {"Location": destination}), (200, {})])

    response = client.get("/initial", headers={"Authorization": EXPLICIT_AUTHORIZATION})

    assert response.status_code == 200
    assert len(adapter.requests) == 2
    assert adapter.requests[0].headers["Authorization"] == EXPLICIT_AUTHORIZATION
    assert adapter.requests[1].headers.get("Authorization") == expected
    for prepared in adapter.requests:
        _assert_no_netrc_secrets(dict(prepared.headers))


@pytest.mark.parametrize("principal", ["none", "admin", "learner"])
def test_managed_sessions_keep_environment_proxy_and_ca_for_initial_and_redirect(tmp_path, monkeypatch, principal):
    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("SYNTHETIC_OFFLINE_CA", encoding="ascii")
    proxy = "http://synthetic-proxy.example.invalid:8080"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_file))
    client = _client(principal, "factory")
    adapter = _mount(client, [(302, {"Location": "/redirected"}), (200, {})])

    response = client.get("/initial")

    assert response.status_code == 200
    assert client.session.trust_env is True
    assert len(adapter.settings) == 2
    for settings in adapter.settings:
        assert settings["proxies"]["https"] == proxy
        assert settings["verify"] == str(ca_file)
    assert all("Authorization" not in item.headers for item in adapter.requests)


@pytest.mark.parametrize("principal", ["none", "admin", "learner"])
def test_managed_transport_errors_have_no_ambient_netrc_credentials(principal, _offline_netrc_environment):
    client = _client(principal)
    adapter = _mount(client, error=requests.ConnectionError("Synthetic offline transport failure"))

    with pytest.raises(requests.ConnectionError) as raised:
        client.get("/error")

    assert len(adapter.requests) == 1
    assert "Authorization" not in adapter.requests[0].headers
    _assert_no_netrc_secrets(raised.value)
    _assert_no_netrc_secrets(_offline_netrc_environment)
    _assert_no_netrc_secrets(Context.all())


def test_anonymous_header_rejection_with_netrc_available_is_secret_safe():
    client = _client("none")
    adapter = _mount(client)

    with pytest.raises(ValueError) as raised:
        client.get("/rejected", headers={"Authorization": EXPLICIT_AUTHORIZATION})

    assert adapter.requests == []
    _assert_no_netrc_secrets(raised.value)
    assert EXPLICIT_AUTHORIZATION not in str(raised.value)


def test_legacy_unspecified_principal_still_uses_requests_netrc_preparation():
    client = _client(None)
    adapter = _mount(client)

    response = client.get("/legacy")

    assert response.status_code == 200
    assert client.principal is None
    assert client.session.trust_env is True
    assert adapter.requests[0].headers["Authorization"] == f"Basic {NETRC_ENCODED}"


def test_legacy_unspecified_principal_still_applies_netrc_on_cross_origin_redirect():
    client = _client(None)
    adapter = _mount(client, [
        (302, {"Location": f"https://{REDIRECT_HOST}/redirected"}),
        (200, {}),
    ])

    response = client.get("/legacy", headers={"Authorization": EXPLICIT_AUTHORIZATION})

    assert response.status_code == 200
    assert len(adapter.requests) == 2
    assert all(item.headers["Authorization"] == f"Basic {NETRC_ENCODED}" for item in adapter.requests)
