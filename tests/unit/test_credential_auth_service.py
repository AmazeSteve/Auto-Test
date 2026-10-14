"""Offline host-bound config lookup and principal-bound login contracts."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter

from wizbank_api_test.core.config import Config
from wizbank_api_test.services.auth_service import AuthService
from wizbank_api_test.utils.encode_util import EncodeUtil


USERS = {
    "admin": {"username": "SYNTHETIC_ADMIN_USERNAME", "password": "SYNTHETIC_ADMIN_PASSWORD"},
    "learner": {"username": "SYNTHETIC_LEARNER_USERNAME", "password": "SYNTHETIC_LEARNER_PASSWORD"},
}
COOKIE = "SESSION=SYNTHETIC_LOGIN_COOKIE; Path=/; HttpOnly"
SECRET = "SYNTHETIC_ERROR_SECRET"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in credential login tests")

    monkeypatch.setattr(HTTPAdapter, "send", deny)


def config(data):
    result = Config.__new__(Config)
    result.data = deepcopy(data)
    return result


class RecordingConfig:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def get_credential_user(self, host_key, principal):
        self.calls.append((host_key, principal))
        if self.failure:
            raise self.failure
        return deepcopy(USERS[principal])


class RecordingClient:
    def __init__(self, host_key="Host-be", principal="admin", status=200, cookie=COOKIE, failure=None):
        self.host_key = host_key
        self.principal = principal
        self.session = requests.Session()
        self.calls = []
        self.status = status
        self.cookie = cookie
        self.failure = failure

    def post(self, path, **kwargs):
        self.calls.append((path, deepcopy(kwargs)))
        if self.failure:
            raise self.failure
        return SimpleNamespace(status_code=self.status, headers={"Set-Cookie": self.cookie})


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_host_be_supports_legacy_user_fallback_and_copies_config(principal):
    original = {"users": deepcopy(USERS)}
    reader = config(original)
    actual = reader.get_credential_user("Host-be", principal)
    assert actual == USERS[principal]
    actual["password"] = "changed"
    assert reader.data == original
    assert reader.get_user(principal) == USERS[principal]


@pytest.mark.parametrize("host_key", ["Host-156", "Host-226", "Other-host", "host-be"])
@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_foreign_host_never_uses_legacy_users(host_key, principal):
    reader = config({"users": USERS})
    with pytest.raises(ValueError, match="Host-bound credential configuration is missing") as raised:
        reader.get_credential_user(host_key, principal)
    assert "SYNTHETIC" not in str(raised.value)


@pytest.mark.parametrize("host_key", ["Host-be", "Host-226"])
@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_exact_explicit_credentials_take_priority(host_key, principal):
    explicit = {"username": "SYNTHETIC_EXPLICIT_USERNAME", "password": "SYNTHETIC_EXPLICIT_PASSWORD"}
    reader = config({"users": USERS, "credentials": {host_key: {principal: explicit}}})
    assert reader.get_credential_user(host_key, principal) == explicit


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_host_be_missing_explicit_principal_can_use_legacy(principal):
    other = "learner" if principal == "admin" else "admin"
    reader = config({"users": USERS, "credentials": {"Host-be": {other: USERS[other]}}})
    assert reader.get_credential_user("Host-be", principal) == USERS[principal]


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_foreign_host_missing_explicit_principal_rejects_even_with_other_users(principal):
    other = "learner" if principal == "admin" else "admin"
    reader = config({"users": USERS, "credentials": {"Host-226": {other: USERS[other]}}})
    with pytest.raises(ValueError):
        reader.get_credential_user("Host-226", principal)


@pytest.mark.parametrize("invalid", [None, {}, {"username": SECRET}, {"password": SECRET},
                                     {"username": SECRET, "password": " "},
                                     {"username": 123, "password": SECRET}])
@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_explicit_invalid_host_be_credentials_do_not_downgrade_to_legacy(invalid, principal):
    reader = config({"users": USERS, "credentials": {"Host-be": {principal: invalid}}})
    with pytest.raises(ValueError, match="Credential configuration is invalid") as raised:
        reader.get_credential_user("Host-be", principal)
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("invalid", [None, [], SECRET])
@pytest.mark.parametrize("location", ["credentials", "host"])
def test_malformed_explicit_credential_schema_fails_safely(invalid, location):
    credentials = invalid if location == "credentials" else {"Host-be": invalid}
    reader = config({"users": USERS, "credentials": credentials})
    with pytest.raises(ValueError) as raised:
        reader.get_credential_user("Host-be", "admin")
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_missing_credential_user_fails_safely(principal):
    with pytest.raises(ValueError):
        config({}).get_credential_user("Host-be", principal)


@pytest.mark.parametrize("host_key,principal", [("", "admin"), (None, "admin"),
                                               ("Host-be", "none"), ("Host-be", None),
                                               ("Host-be", "ADMIN")])
def test_config_rejects_unsupported_credential_identity(host_key, principal):
    with pytest.raises(ValueError):
        config({"users": USERS}).get_credential_user(host_key, principal)


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_matching_bound_login_preserves_wire_payload_and_literal_values(principal):
    username = "{{SYNTHETIC_LITERAL_USERNAME}}"
    password = "$data.synthetic_literal_password"
    reader = config({"users": {principal: {"username": username, "password": password}}})
    original = deepcopy(reader.data)
    client = RecordingClient(principal=principal)
    service = AuthService(client, reader, principal=principal)

    assert getattr(service, principal + "_login")() == COOKIE
    assert client.calls == [("/app/user/login/v3", {
        "params": {"developer": "PC"},
        "json": {"eipCode": "", "usrSteUsrId": username,
                 "userPassword": EncodeUtil.base64_encode(password), "loginModeV2": "PC",
                 "curLan": "zh-cn", "qrcodeType": "", "qrcodeItmType": "", "qrcodeencItmId": ""},
        "literal_json": True,
    })]
    assert reader.data == original


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_foreign_bound_login_uses_only_explicit_host_credentials(principal):
    explicit = {"username": "SYNTHETIC_FOREIGN_USERNAME", "password": "SYNTHETIC_FOREIGN_PASSWORD"}
    client = RecordingClient(host_key="Host-226", principal=principal)
    reader = config({"users": USERS, "credentials": {"Host-226": {principal: explicit}}})
    service = AuthService(client, reader, host_key="Host-226", principal=principal)
    assert getattr(service, principal + "_login")() == COOKIE
    assert client.calls[0][1]["json"]["usrSteUsrId"] == explicit["username"]
    assert client.calls[0][1]["json"]["userPassword"] == EncodeUtil.base64_encode(explicit["password"])


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_foreign_bound_login_without_explicit_credentials_never_calls_transport(principal):
    client = RecordingClient(host_key="Host-226", principal=principal)
    service = AuthService(client, config({"users": USERS}), host_key="Host-226", principal=principal)
    with pytest.raises(ValueError, match="Authentication login failed"):
        getattr(service, principal + "_login")()
    assert client.calls == []


@pytest.mark.parametrize("bound,attempted", [("admin", "learner"), ("learner", "admin")])
def test_bound_method_mismatch_fails_before_configuration_and_transport(bound, attempted):
    reader = RecordingConfig(AssertionError(SECRET))
    client = RecordingClient(principal=bound)
    service = AuthService(client, reader, principal=bound)
    with pytest.raises(ValueError, match="Authentication service principal mismatch") as raised:
        getattr(service, attempted + "_login")()
    assert reader.calls == client.calls == []
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("host_key,principal", [("Host-226", "admin"), ("Host-be", "learner"),
                                               ("Host-be", "none"), (None, "admin")])
def test_client_identity_mismatch_fails_before_configuration_and_transport(host_key, principal):
    reader = RecordingConfig(AssertionError(SECRET))
    client = RecordingClient(host_key=host_key, principal=principal)
    service = AuthService(client, reader)
    with pytest.raises(ValueError) as raised:
        service.admin_login()
    assert reader.calls == client.calls == []
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("principal", ["admin", "learner"])
@pytest.mark.parametrize("failure_kind", ["config", "transport", "status", "missing_cookie", "blank_cookie", "invalid_cookie"])
def test_login_failure_exceptions_never_include_secret_or_chained_cause(principal, failure_kind):
    reader = RecordingConfig(RuntimeError(SECRET) if failure_kind == "config" else None)
    client = RecordingClient(
        principal=principal,
        failure=RuntimeError(SECRET) if failure_kind == "transport" else None,
        status=401 if failure_kind == "status" else 200,
        cookie={"missing_cookie": None, "blank_cookie": " ", "invalid_cookie": [SECRET]}.get(failure_kind, COOKIE),
    )
    service = AuthService(client, reader, principal=principal)
    with pytest.raises(ValueError, match="^Authentication login failed$") as raised:
        getattr(service, principal + "_login")()
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert reader.calls == [("Host-be", principal)]
    assert len(client.calls) == (0 if failure_kind == "config" else 1)


def test_compatibility_auth_service_constructor_is_host_be_admin_bound():
    service = AuthService(RecordingClient(), RecordingConfig())
    assert (service.host_key, service.principal) == ("Host-be", "admin")
    assert service.admin_login() == COOKIE
