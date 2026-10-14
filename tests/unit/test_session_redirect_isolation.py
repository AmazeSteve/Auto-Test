"""Exercise Requests preparation and redirects with an offline adapter."""

from collections import deque
from email.message import Message
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from requests.structures import CaseInsensitiveDict

from wizbank_api_test.tests.conftest import api_client_factory


class _Config:
    def get_base_url(self, host_key):
        return "https://offline.invalid"

    def get_default_headers(self):
        return {"Accept": "application/json"}

    def get_timeout(self):
        return 1

    def get_verify_ssl(self):
        return True


class _OfflineAdapter(BaseAdapter):
    """Return real Responses whose raw metadata Requests uses for cookies."""

    def __init__(self, responses):
        super().__init__()
        self.responses = deque(responses)
        self.requests = []

    def send(self, request, **kwargs):
        assert isinstance(request, requests.PreparedRequest)
        assert self.responses, "Unexpected request reached offline transport"
        self.requests.append(request)
        status, headers = self.responses.popleft()
        message = Message()
        for name, value in headers.items():
            message.add_header(name, value)
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict(headers)
        response.url = request.url
        response.request = request
        response._content = b"{}"
        response._content_consumed = True
        response.raw = SimpleNamespace(
            _original_response=SimpleNamespace(msg=message),
            close=lambda: None,
            release_conn=lambda: None,
        )
        return response

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reject_real_transport(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Real HTTP transport is forbidden in this unit test")

    monkeypatch.setattr(HTTPAdapter, "send", reject)


@pytest.fixture
def factory():
    return api_client_factory.__wrapped__(_Config())


def _mount(client, responses):
    assert isinstance(client.session, requests.Session)
    adapter = _OfflineAdapter(responses)
    client.session.mount("https://offline.invalid/", adapter)
    return adapter


def _admin_cookie(client, value="OLD_ADMIN"):
    client.session.cookies.set(
        "admin_session", value, domain="offline.invalid", path="/"
    )


def test_explicit_admin_cookie_initial_preparation_does_not_duplicate_jar(factory):
    admin = factory("Host-be", "admin")
    _admin_cookie(admin)
    adapter = _mount(admin, [(200, {})])

    response = admin.get(
        "/initial", headers={"Cookie": "admin_session=EXPLICIT_ADMIN"}
    )

    assert response.status_code == 200
    assert len(adapter.requests) == 1
    prepared = adapter.requests[0]
    assert prepared.headers["Cookie"] == "admin_session=EXPLICIT_ADMIN"
    assert prepared.headers["Cookie"].count("admin_session=") == 1
    assert "OLD_ADMIN" not in prepared.headers["Cookie"]
    assert admin.session.cookies.get("admin_session") == "OLD_ADMIN"


def test_admin_redirect_rebuilds_cookie_from_own_session_jar(factory):
    admin = factory("Host-be", "admin")
    _admin_cookie(admin)
    adapter = _mount(admin, [(302, {"Location": "/redirected"}), (200, {})])

    response = admin.get(
        "/initial", headers={"Cookie": "admin_session=EXPLICIT_ADMIN"}
    )

    assert response.status_code == 200
    assert [item.status_code for item in response.history] == [302]
    assert [item.headers.get("Cookie") for item in adapter.requests] == [
        "admin_session=EXPLICIT_ADMIN",
        "admin_session=OLD_ADMIN",
    ]
    assert adapter.requests[1].url == "https://offline.invalid/redirected"
    assert admin.session.cookies.get("admin_session") == "OLD_ADMIN"
    assert factory("Host-be", "learner").session.cookies.get_dict() == {}
    assert factory("Host-be", "none").session.cookies.get_dict() == {}


def test_admin_redirect_extracts_set_cookie_through_requests_cookie_jar(factory):
    admin = factory("Host-be", "admin")
    _admin_cookie(admin)
    adapter = _mount(
        admin,
        [
            (
                302,
                {
                    "Location": "/redirected",
                    "Set-Cookie": "admin_session=NEW_ADMIN; Path=/; HttpOnly",
                },
            ),
            (200, {}),
        ],
    )

    response = admin.get(
        "/initial", headers={"Cookie": "admin_session=EXPLICIT_ADMIN"}
    )

    assert response.status_code == 200
    assert [item.headers.get("Cookie") for item in adapter.requests] == [
        "admin_session=EXPLICIT_ADMIN",
        "admin_session=NEW_ADMIN",
    ]
    assert admin.session.cookies.get("admin_session") == "NEW_ADMIN"
    assert factory("Host-be", "none").session.cookies.get_dict() == {}


def test_anonymous_redirect_cookie_is_limited_to_current_top_level_session(factory):
    admin = factory("Host-be", "admin")
    _admin_cookie(admin)
    anonymous = factory("Host-be", "none")
    assert anonymous.session is not admin.session
    adapter = _mount(
        anonymous,
        [
            (
                302,
                {
                    "Location": "/second",
                    "Set-Cookie": "chain_session=CHAIN_ANON; Path=/; HttpOnly",
                },
            ),
            (302, {"Location": "/final"}),
            (200, {}),
        ],
    )

    response = anonymous.get("/initial")

    assert response.status_code == 200
    assert [item.status_code for item in response.history] == [302, 302]
    assert [item.headers.get("Cookie") for item in adapter.requests] == [
        None,
        "chain_session=CHAIN_ANON",
        "chain_session=CHAIN_ANON",
    ]
    assert anonymous.session.cookies.get("chain_session") == "CHAIN_ANON"
    assert admin.session.cookies.get_dict() == {"admin_session": "OLD_ADMIN"}

    next_anonymous = factory("Host-be", "none")
    assert next_anonymous is not anonymous
    assert next_anonymous.session is not anonymous.session
    assert next_anonymous.session.cookies.get_dict() == {}
    next_adapter = _mount(next_anonymous, [(200, {})])
    next_response = next_anonymous.get("/next-top-level")
    assert next_response.status_code == 200
    assert next_adapter.requests[0].headers.get("Cookie") is None
    assert anonymous.session.cookies.get("chain_session") == "CHAIN_ANON"
    assert admin.session.cookies.get_dict() == {"admin_session": "OLD_ADMIN"}


@pytest.mark.parametrize("principal", ["admin", "none"])
def test_allow_redirects_false_does_not_send_redirect_request(factory, principal):
    client = factory("Host-be", principal)
    if principal == "admin":
        _admin_cookie(client)
    adapter = _mount(client, [(302, {"Location": "/must-not-be-sent"})])

    response = client.get("/initial", allow_redirects=False)

    assert response.status_code == 302
    assert response.history == []
    assert len(adapter.requests) == 1
    assert adapter.requests[0].url == "https://offline.invalid/initial"
    expected_cookie = "admin_session=OLD_ADMIN" if principal == "admin" else None
    assert adapter.requests[0].headers.get("Cookie") == expected_cookie
