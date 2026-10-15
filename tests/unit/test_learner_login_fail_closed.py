"""Learner login cannot access credentials or pollute the admin Session offline."""

import json
from copy import deepcopy

import allure
import pytest
from requests.adapters import HTTPAdapter

from wizbank_api_test.core.context import Context
from wizbank_api_test.tests import conftest as fixtures
from wizbank_api_test.tests.unit.test_session_ownership import FakeConfig, attach, factory
from wizbank_api_test.utils.encode_util import EncodeUtil


ADMIN_COOKIE_SECRET = "SYNTHETIC_ADMIN_COOKIE_SECRET"
AUTHORIZATION_SECRET = "SYNTHETIC_ADMIN_AUTHORIZATION_SECRET"
LEARNER_PASSWORD_SECRET = "SYNTHETIC_LEARNER_PASSWORD_SECRET"


@pytest.fixture(autouse=True)
def offline(monkeypatch, bind_context):
    Context.clear()

    def deny(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in learner login tests")

    monkeypatch.setattr(HTTPAdapter, "send", deny)
    yield
    Context.clear()


def test_auth_service_compatibility_fixture_owns_host_be_admin_client():
    config = FakeConfig()
    get_client = factory(config)

    service = fixtures.auth_service.__wrapped__(get_client, config)

    assert service.config is config
    assert service.api_client is get_client("Host-be", "admin")
    assert service.api_client.principal == "admin"
    assert service.api_client.base_url == "https://be.example.invalid"
    assert service.api_client is not get_client("Host-226", "admin")


@pytest.mark.parametrize("attempts", [1, 2], ids=["first-attempt", "repeated-attempt"])
def test_learner_login_fails_before_credentials_and_transport_without_admin_pollution(monkeypatch, attempts):
    config = FakeConfig({"Authorization": AUTHORIZATION_SECRET})
    get_client = factory(config)
    service = fixtures.auth_service.__wrapped__(get_client, config)
    admin = get_client("Host-be", "admin")
    admin.session.cookies.set("SESSION", ADMIN_COOKIE_SECRET, domain="be.example.invalid", path="/")
    adapter = attach(admin)
    original_cookies = deepcopy([vars(cookie) for cookie in admin.session.cookies])
    original_context = deepcopy(Context.all())
    evidence = []
    credential_calls = []
    request_calls = []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append(str(body)))

    def forbidden_credentials(host_key, principal):
        credential_calls.append((host_key, principal))
        raise AssertionError(LEARNER_PASSWORD_SECRET)

    def forbidden_request(*args, **kwargs):
        request_calls.append((args, kwargs))
        raise AssertionError(ADMIN_COOKIE_SECRET)

    monkeypatch.setattr(config, "get_credential_user", forbidden_credentials)
    monkeypatch.setattr(admin, "post", forbidden_request)
    for _ in range(attempts):
        with pytest.raises(ValueError) as raised:
            service.learner_login()
        assert "principal" in str(raised.value).lower()
        for secret in (ADMIN_COOKIE_SECRET, AUTHORIZATION_SECRET, LEARNER_PASSWORD_SECRET):
            assert secret not in str(raised.value)

    assert credential_calls == []
    assert request_calls == []
    assert adapter.requests == []
    assert [vars(cookie) for cookie in admin.session.cookies] == original_cookies
    assert Context.all() == original_context
    assert evidence == []
    for secret in (ADMIN_COOKIE_SECRET, AUTHORIZATION_SECRET, LEARNER_PASSWORD_SECRET):
        assert secret not in repr(Context.all())
        assert secret not in repr(evidence)


def test_admin_login_keeps_existing_request_and_cookie_behavior_offline(monkeypatch):
    config = FakeConfig({"Authorization": AUTHORIZATION_SECRET})
    get_client = factory(config)
    service = fixtures.auth_service.__wrapped__(get_client, config)
    admin = get_client("Host-be", "admin")
    cookie = f"SESSION={ADMIN_COOKIE_SECRET}; Path=/; HttpOnly"
    adapter = attach(admin, cookie)
    evidence = []
    original_context = deepcopy(Context.all())
    credential_calls = []
    original_get_user = config.get_credential_user

    def record_credentials(host_key, principal):
        credential_calls.append(principal)
        return original_get_user(host_key, principal)

    monkeypatch.setattr(config, "get_credential_user", record_credentials)
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append(str(body)))

    provider = fixtures.credential_provider.__wrapped__(get_client, config)
    assert fixtures.admin_cookie.__wrapped__(provider) == cookie

    assert credential_calls == ["admin"]
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.url == "https://be.example.invalid/app/user/login/v3?developer=PC"
    assert request.method == "POST"
    assert request.headers["Authorization"] == AUTHORIZATION_SECRET
    assert json.loads(request.body) == {
        "eipCode": "", "usrSteUsrId": "offline-admin",
        "userPassword": EncodeUtil.base64_encode("offline-password"),
        "loginModeV2": "PC", "curLan": "zh-cn", "qrcodeType": "",
        "qrcodeItmType": "", "qrcodeencItmId": "",
    }
    assert admin.session.cookies.get("SESSION") == ADMIN_COOKIE_SECRET
    assert Context.all() == original_context
    for secret in (ADMIN_COOKIE_SECRET, AUTHORIZATION_SECRET, "offline-password", EncodeUtil.base64_encode("offline-password")):
        assert secret not in repr(Context.all())
        assert secret not in repr(evidence)
