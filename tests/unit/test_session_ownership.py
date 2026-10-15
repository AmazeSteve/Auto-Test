"""Offline Session ownership and compatibility-fixture regression tests."""

from copy import deepcopy
from email.message import Message
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter

from wizbank_api_test.core.context import Context
from wizbank_api_test.tests import conftest as fixtures


class FakeConfig:
    """Only synthetic inputs; these tests never load real environment credentials."""

    def __init__(self, default_headers=None):
        self.default_headers = default_headers or {"X-Offline": "true"}

    def get_base_url(self, host_key):
        return {
            "Host-be": "https://be.example.invalid",
            "Host-226": "https://other.example.invalid",
        }[host_key]

    def get_default_headers(self):
        return self.default_headers

    def get_timeout(self):
        return 7

    def get_verify_ssl(self):
        return True

    def get_user(self, principal):
        assert principal == "admin"
        return {"username": "offline-admin", "password": "offline-password"}

    def get_credential_user(self, host_key, principal):
        assert host_key == "Host-be"
        return self.get_user(principal)


class CookieAdapter(BaseAdapter):
    """Let real Requests prepare requests and extract response cookies offline."""

    def __init__(self, cookie=None):
        self.cookie = cookie
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        response._content = b'{"code": 200}'
        response.encoding = "utf-8"
        message = Message()
        if self.cookie:
            response.headers["Set-Cookie"] = self.cookie
            message["Set-Cookie"] = self.cookie
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
        return response

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_real_transport(monkeypatch, bind_context):
    Context.clear()

    def deny(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in Session ownership tests")

    monkeypatch.setattr(HTTPAdapter, "send", deny)
    yield
    Context.clear()


def factory(config=None):
    return fixtures.api_client_factory.__wrapped__(config or FakeConfig())


def attach(client, cookie=None):
    adapter = CookieAdapter(cookie)
    client.session.mount("https://", adapter)
    return adapter


def test_factory_isolates_principals_and_hosts_and_caches_only_authenticated_clients():
    get_client = factory()
    admin = get_client("Host-be", "admin")
    learner = get_client("Host-be", "learner")
    other_admin = get_client("Host-226", "admin")
    anonymous = get_client("Host-be", "none")

    assert get_client("Host-be", "admin") is admin
    assert get_client("Host-be", "learner") is learner
    assert get_client("Host-226", "admin") is other_admin
    assert len({id(client) for client in (admin, learner, other_admin, anonymous)}) == 4
    assert len({id(client.session) for client in (admin, learner, other_admin, anonymous)}) == 4
    assert get_client("Host-be", "none") is not anonymous


@pytest.mark.parametrize("arguments", [(), ("Host-be",), ("Host-be", "none"), ("Host-be", None)])
def test_compatible_anonymous_factory_calls_always_have_fresh_sessions(arguments):
    get_client = factory()
    first, second = get_client(*arguments), get_client(*arguments)
    assert first is not second
    assert first.session is not second.session
    assert not first.session.cookies
    assert not second.session.cookies


def test_admin_login_fixture_owns_admin_session_and_cannot_pollute_anonymous():
    config = FakeConfig()
    get_client = factory(config)
    service = fixtures.auth_service.__wrapped__(get_client, config)
    admin = get_client("Host-be", "admin")
    assert service.api_client is admin
    adapter = attach(admin, "SESSION=OFFLINE_ADMIN; Path=/; HttpOnly")
    original_context = Context.all()

    provider = fixtures.credential_provider.__wrapped__(get_client, config)
    assert adapter.requests == []
    cookie = fixtures.admin_cookie.__wrapped__(provider)

    assert cookie == "SESSION=OFFLINE_ADMIN; Path=/; HttpOnly"
    assert admin.session.cookies.get("SESSION") == "OFFLINE_ADMIN"
    assert adapter.requests[0].url == "https://be.example.invalid/app/user/login/v3?developer=PC"
    assert Context.all() == original_context

    anonymous = get_client("Host-be", "none")
    anonymous_adapter = attach(anonymous)
    anonymous.get("/offline-read")
    assert "Cookie" not in anonymous_adapter.requests[0].headers
    assert "Authorization" not in anonymous_adapter.requests[0].headers
    assert not anonymous.session.cookies


def test_anonymous_response_cookie_is_not_inherited_by_next_top_level_request():
    get_client = factory()
    first = get_client("Host-be", "none")
    attach(first, "VISITOR=FIRST_CALL; Path=/")
    first.get("/offline-read")
    assert first.session.cookies.get("VISITOR") == "FIRST_CALL"

    second = get_client("Host-be", "none")
    adapter = attach(second)
    second.get("/offline-read")
    assert not second.session.cookies
    assert "Cookie" not in adapter.requests[0].headers
    assert first.session.cookies.get("VISITOR") == "FIRST_CALL"


def test_admin_cookie_persistence_stays_with_same_principal_and_host():
    get_client = factory()
    admin = get_client("Host-be", "admin")
    attach(admin, "SESSION=SAME_ADMIN; Path=/")
    admin.get("/offline-read")
    cached = get_client("Host-be", "admin")
    adapter = attach(cached)
    cached.get("/offline-read")
    assert adapter.requests[0].headers["Cookie"] == "SESSION=SAME_ADMIN"

    for host, principal in (("Host-be", "learner"), ("Host-be", "none"), ("Host-226", "admin")):
        isolated = get_client(host, principal)
        prepared = isolated.session.prepare_request(requests.Request("GET", isolated.base_url))
        assert "Cookie" not in prepared.headers
        assert not isolated.session.cookies


def test_user_group_fixture_preserves_admin_client_and_explicit_cookie_behavior():
    get_client = factory()
    cookie = "SESSION=OFFLINE_EXPLICIT_ADMIN"
    service = fixtures.user_group_service.__wrapped__(cookie, get_client)
    assert service.api_client is get_client("Host-be", "admin")
    assert service.admin_cookie == cookie
    assert service._headers() == {"Cookie": cookie}
    assert not Context.all()["variables"]


def test_compatibility_api_client_fixture_is_function_scoped_and_anonymous():
    assert fixtures.api_client._fixture_function_marker.scope == "function"
    calls = []
    sentinel = object()

    def record(host, principal):
        calls.append((host, principal))
        return sentinel

    assert fixtures.api_client.__wrapped__(record) is sentinel
    assert calls == [("Host-be", "none")]


@pytest.mark.parametrize("header", ["Cookie", "cOoKiE", "Authorization", "aUtHoRiZaTiOn"])
@pytest.mark.parametrize("principal", ["none", None])
def test_anonymous_default_auth_headers_are_rejected_without_leaking_values(header, principal):
    secret = "OFFLINE_AUTH_SECRET"
    config = FakeConfig({header: secret, "X-Offline": "true"})
    original = deepcopy(config.default_headers)
    get_client = factory(config)
    with pytest.raises(ValueError) as caught:
        get_client("Host-be", principal)
    assert secret not in str(caught.value)
    assert config.default_headers == original


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_authenticated_default_header_behavior_remains_unchanged(principal):
    defaults = {"Cookie": "OFFLINE_COOKIE", "Authorization": "Bearer OFFLINE_TOKEN"}
    config = FakeConfig(defaults)
    client = factory(config)("Host-be", principal)
    assert client.default_headers == defaults
    assert client.timeout == 7
    assert client.verify_ssl is True
    assert config.default_headers == defaults
