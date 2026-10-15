"""Credential identity through real Requests preparation and offline transport."""

import json
import traceback
from copy import deepcopy
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlsplit

import allure
import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter

from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.client import redact_auth_evidence, sanitize_auth_exception
from wizbank_api_test.core.config import Config
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.services.credential_provider import Credential, CredentialProvider
from wizbank_api_test.tests import conftest as fixtures
from wizbank_api_test.tests.unit.test_request_payload_resolver import builder_config, read_model
from wizbank_api_test.utils.encode_util import EncodeUtil
from wizbank_api_test.utils.recovery_journal import RecoveryJournal
from wizbank_api_test.utils.test_data_factory import TestDataFactory as DataFactory


ADMIN_USER = "RUNTIME_ADMIN_USERNAME_CANARY"
ADMIN_PASSWORD = "RUNTIME_ADMIN_PASSWORD_CANARY"
LEARNER_USER = "RUNTIME_LEARNER_USERNAME_CANARY"
LEARNER_PASSWORD = "RUNTIME_LEARNER_PASSWORD_CANARY"
ADMIN_COOKIE = "RUNTIME_ADMIN_COOKIE_CANARY"
LEARNER_COOKIE = "RUNTIME_LEARNER_COOKIE_CANARY"
AUTHORIZATION = "Bearer RUNTIME_AUTHORIZATION_CANARY"
FOREIGN_USER = "RUNTIME_FOREIGN_USERNAME_CANARY"
FOREIGN_PASSWORD = "RUNTIME_FOREIGN_PASSWORD_CANARY"
FOREIGN_COOKIE = "RUNTIME_FOREIGN_COOKIE_CANARY"


class OfflineAdapter(BaseAdapter):
    """Requests prepares headers, follows redirects and extracts real cookie metadata."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def send(self, request, **kwargs):
        assert isinstance(request, requests.PreparedRequest)
        self.requests.append(request)
        status, headers, payload = self.handler(request)
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.request = request
        response.url = request.url
        response.encoding = "utf-8"
        response._content = (
            payload.encode("utf-8") if isinstance(payload, str)
            else json.dumps(payload).encode("utf-8")
        )
        response._content_consumed = True
        message = Message()
        for name, value in headers.items():
            message.add_header(name, value)
        response.raw = SimpleNamespace(
            _original_response=SimpleNamespace(msg=message),
            close=lambda: None, release_conn=lambda: None,
        )
        return response

    def close(self):
        pass


class RecordingProvider(CredentialProvider):
    def __init__(self, factory, config):
        super().__init__(factory, config)
        self.get_calls = []

    def get(self, host_key, principal):
        self.get_calls.append((host_key, principal))
        return super().get(host_key, principal)


@pytest.fixture(autouse=True)
def offline(monkeypatch, bind_context, tmp_path):
    Context.clear()

    def deny(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in credential Runtime tests")

    monkeypatch.setattr(HTTPAdapter, "send", deny)
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


def environment(*, defaults=None, credentials=None, cookies=None, handler=None, login_payload=None):
    config = Config.__new__(Config)
    config.data = {
        "base_urls": {"Host-be": "https://be.example.invalid", "Host-226": "https://foreign.example.invalid"},
        "users": {
            "admin": {"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            "learner": {"username": LEARNER_USER, "password": LEARNER_PASSWORD},
        },
        "default_headers": deepcopy(defaults or {}),
        "credentials": deepcopy(credentials or {}),
        "request": {"timeout": 1, "verify_ssl": True},
    }
    cookie_values = {
        ("Host-be", "admin"): f"SESSION={ADMIN_COOKIE}; Path=/; HttpOnly",
        ("Host-be", "learner"): f"SESSION={LEARNER_COOKIE}; Path=/; HttpOnly",
        ("Host-226", "admin"): f"SESSION={FOREIGN_COOKIE}; Path=/; HttpOnly",
    }
    cookie_values.update(cookies or {})
    base_factory = fixtures.api_client_factory.__wrapped__(config)
    adapters = {}

    def response_for(identity, request):
        if urlsplit(request.url).path == "/app/user/login/v3":
            return 200, {"Set-Cookie": cookie_values[identity]}, deepcopy(login_payload or {"code": 200})
        if handler is not None:
            return handler(identity, request)
        return 200, {}, {"code": 200, "data": {"value": "ordinary captured value"}}

    def factory(host_key="Host-be", principal="none"):
        client = base_factory(host_key, principal)
        if client not in adapters:
            adapter = OfflineAdapter(lambda request: response_for((host_key, client.principal), request))
            client.session.mount("https://", adapter)
            adapters[client] = adapter
        return client

    return config, factory, RecordingProvider(factory, config), adapters, cookie_values


def case_for(principal, host="Host-be", path="/offline-runtime"):
    return {
        "id": "CREDENTIAL_RUNTIME", "method": "GET", "path": path,
        "host": host, "auth": principal,
        "validate": [{"type": "status_code", "expected": 200}],
    }


def configure_capture(monkeypatch, *, principal="learner", host="Host-be", path="/offline-capture"):
    endpoint = {"method": "GET", "path": path, "host": host, "auth": principal}
    monkeypatch.setattr(EndpointRegistry, "get", lambda self, endpoint_id: deepcopy(endpoint))


def login_requests(adapter):
    return [item for item in adapter.requests if urlsplit(item.url).path == "/app/user/login/v3"]


def contains_credential(value):
    if isinstance(value, Credential):
        return True
    if isinstance(value, dict):
        return any(contains_credential(key) or contains_credential(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_credential(item) for item in value)
    return False


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_runtime_legacy_login_uses_matching_session_and_provider_cookie_overrides_stale_headers(principal):
    config, factory, provider, adapters, cookies = environment(defaults={"cOoKiE": "STALE_DEFAULT"})
    client = factory("Host-be", principal)
    other = factory("Host-be", "learner" if principal == "admin" else "admin")
    case = case_for(principal)
    case["headers"] = {"COOKIE": "{{stale_yaml_cookie_must_not_be_resolved}}", "X-Ordinary": "unchanged"}
    original = deepcopy(case)

    CaseRunner.run(case, factory, provider, apply_metadata=False)

    requests_sent = adapters[client].requests
    assert len(requests_sent) == 2
    expected_user = config.data["users"][principal]
    assert json.loads(requests_sent[0].body)["usrSteUsrId"] == expected_user["username"]
    assert json.loads(requests_sent[0].body)["userPassword"] == EncodeUtil.base64_encode(expected_user["password"])
    assert requests_sent[1].headers["Cookie"] == cookies[("Host-be", principal)]
    assert "STALE_DEFAULT" not in requests_sent[1].headers["Cookie"]
    assert requests_sent[1].headers["X-Ordinary"] == "unchanged"
    credential = provider.get("Host-be", principal)
    assert provider.get("Host-be", principal) is credential
    assert len(login_requests(adapters[client])) == 1
    assert client.session is not other.session
    assert other.session.cookies.get_dict() == {}
    assert adapters[other].requests == []
    assert Context.all()["variables"] == {}
    assert not contains_credential(Context.all())
    assert case == original


def test_explicit_foreign_admin_uses_foreign_config_and_session_only():
    credentials = {"Host-226": {"admin": {"username": FOREIGN_USER, "password": FOREIGN_PASSWORD}}}
    _, factory, provider, adapters, cookies = environment(credentials=credentials)
    foreign = factory("Host-226", "admin")
    host_be = factory("Host-be", "admin")

    CaseRunner.run(case_for("admin", "Host-226"), factory, provider, apply_metadata=False)

    assert all(urlsplit(item.url).hostname == "foreign.example.invalid" for item in adapters[foreign].requests)
    login_body = json.loads(login_requests(adapters[foreign])[0].body)
    assert login_body["usrSteUsrId"] == FOREIGN_USER
    assert login_body["userPassword"] == EncodeUtil.base64_encode(FOREIGN_PASSWORD)
    assert ADMIN_USER not in repr(login_body)
    assert ADMIN_PASSWORD not in repr(login_body)
    assert adapters[foreign].requests[-1].headers["Cookie"] == cookies[("Host-226", "admin")]
    assert adapters[host_be].requests == []
    assert host_be.session.cookies.get_dict() == {}
    assert foreign.session is not host_be.session
    assert set(provider._cache) == {("Host-226", "admin")}


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_runtime_redirect_keeps_principal_session_and_cached_credential(principal):
    redirected_value = "REDIRECT_JAR_" + principal

    def handler(identity, request):
        if urlsplit(request.url).path == "/offline-start":
            return 302, {"Location": "/offline-final", "Set-Cookie": f"SESSION={redirected_value}; Path=/"}, {}
        return 200, {}, {"code": 200}

    _, factory, provider, adapters, cookies = environment(handler=handler)
    client = factory("Host-be", principal)
    session = client.session
    other = factory("Host-be", "learner" if principal == "admin" else "admin")
    CaseRunner.run(case_for(principal, path="/offline-start"), factory, provider, apply_metadata=False)

    sent = adapters[client].requests
    assert len(sent) == 3
    assert sent[1].headers["Cookie"] == cookies[("Host-be", principal)]
    assert sent[2].headers["Cookie"] == f"SESSION={redirected_value}"
    assert client.session is session
    credential = provider._cache[("Host-be", principal)]
    assert provider.get("Host-be", principal) is credential
    assert credential.cookie == cookies[("Host-be", principal)]
    assert len(login_requests(adapters[client])) == 1
    assert provider._sessions[("Host-be", principal)] is session
    assert other.session.cookies.get_dict() == {}
    assert adapters[other].requests == []
    assert set(provider._cache) == {("Host-be", principal)}


@pytest.mark.parametrize("cookie", ["SESSION={{literal}}", "$timestamp_ms", "$data.private_cookie"])
def test_provider_cookie_is_literal_and_never_entered_into_variable_or_test_data_resolution(monkeypatch, cookie):
    _, factory, provider, adapters, _ = environment(cookies={("Host-be", "admin"): cookie})
    client = factory("Host-be", "admin")
    resolved_values = []
    original_resolve = VariableResolver.resolve

    def resolve(cls, value):
        resolved_values.append(value)
        return original_resolve(value)

    def forbidden(*args, **kwargs):
        raise AssertionError("Credential data must never reach runtime value generation")

    monkeypatch.setattr(VariableResolver, "resolve", classmethod(resolve))
    monkeypatch.setattr(VariableResolver, "timestamp_ms", forbidden)
    monkeypatch.setattr(DataFactory, "resolve", forbidden)
    CaseRunner.run(case_for("admin"), factory, provider, apply_metadata=False)

    assert adapters[client].requests[-1].headers["Cookie"] == cookie
    assert cookie not in [value for value in resolved_values if isinstance(value, str)]
    assert provider._cache[("Host-be", "admin")].cookie == cookie
    assert Context.all()["variables"] == {}


@pytest.mark.parametrize("root_principal", ["admin", "none"])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_normalized_learner_capture_uses_learner_login_and_session_without_root_admin_login(monkeypatch, root_principal, phase):
    configure_capture(monkeypatch)
    _, factory, provider, adapters, cookies = environment()
    learner = factory("Host-be", "learner")
    admin = factory("Host-be", "admin")
    result = EffectExecutor.execute_capture(
        case_for(root_principal), {"endpoint": "OFFLINE_READ"}, phase, factory, provider, CaseRunner.build_header,
    )

    assert result == {"code": 200, "data": {"value": "ordinary captured value"}}
    assert len(login_requests(adapters[learner])) == 1
    assert adapters[learner].requests[-1].headers["Cookie"] == cookies[("Host-be", "learner")]
    assert adapters[admin].requests == []
    assert admin.session.cookies.get_dict() == {}
    assert admin.session is not learner.session
    assert provider.get_calls == [("Host-be", "learner")]
    assert set(provider._cache) == {("Host-be", "learner")}
    assert not contains_credential(Context.all())


@pytest.mark.parametrize("principal", ["none", None])
def test_anonymous_capture_does_not_lookup_credentials_or_login_and_gets_fresh_session(monkeypatch, principal):
    configure_capture(monkeypatch, principal=principal)
    _, factory, provider, adapters, _ = environment()
    EffectExecutor.execute_capture(case_for("admin"), {"endpoint": "OFFLINE_READ"}, "before", factory, provider, CaseRunner.build_header)
    first = next(iter(adapters))
    EffectExecutor.execute_capture(case_for("admin"), {"endpoint": "OFFLINE_READ"}, "after", factory, provider, CaseRunner.build_header)
    second = list(adapters)[-1]

    assert first is not second
    assert first.session is not second.session
    assert first.principal == second.principal == "none"
    assert provider.get_calls == []
    assert provider._cache == {}
    assert all(len(adapter.requests) == 1 and login_requests(adapter) == [] for adapter in adapters.values())
    assert all("Cookie" not in adapter.requests[0].headers for adapter in adapters.values())


@pytest.mark.parametrize("principal", ["admin", "learner"])
@pytest.mark.parametrize("entry", ["case", "capture"])
def test_different_runtime_and_provider_factories_fail_before_any_login_or_transport(monkeypatch, principal, entry):
    _, runtime_factory, _, runtime_adapters, _ = environment()
    _, _, provider, provider_adapters, _ = environment()
    if entry == "capture":
        configure_capture(monkeypatch, principal=principal)

    with pytest.raises(ValueError) as raised:
        if entry == "case":
            CaseRunner.run(case_for(principal), runtime_factory, provider, apply_metadata=False)
        else:
            EffectExecutor.execute_capture(case_for("none"), {"endpoint": "OFFLINE_READ"}, "before", runtime_factory, provider, CaseRunner.build_header)

    assert provider.get_calls == []
    assert provider._cache == {}
    assert all(adapter.requests == [] for adapter in (*runtime_adapters.values(), *provider_adapters.values()))
    assert ADMIN_COOKIE not in str(raised.value)
    assert LEARNER_COOKIE not in str(raised.value)
    assert Context.snapshot()["size"] == 0


@pytest.mark.parametrize("principal", ["admin", "learner"])
@pytest.mark.parametrize("entry", ["case", "header"])
def test_authenticated_runtime_rejects_naked_cookie_without_transport(principal, entry):
    _, factory, provider, adapters, _ = environment()
    case = case_for(principal)
    with pytest.raises(ValueError) as raised:
        if entry == "case":
            CaseRunner.run(case, factory, ADMIN_COOKIE, apply_metadata=False)
        else:
            CaseRunner.build_header(case, ADMIN_COOKIE)
    assert ADMIN_COOKIE not in str(raised.value)
    assert provider.get_calls == []
    assert provider._cache == {}
    assert all(adapter.requests == [] for adapter in adapters.values())
    assert Context.snapshot()["size"] == 0


def test_typed_credentials_never_enter_context_effect_or_recovery_material(monkeypatch, tmp_path, evidence):
    model = read_model()

    def handler(identity, request):
        if urlsplit(request.url).path == "/offline-update":
            model["usr_display_bil"] = json.loads(request.body)["usr_display_bil"]
            return 200, {}, {"code": 200}
        return 200, {}, {"code": 200, "data": deepcopy(model)}

    configure_capture(monkeypatch, principal="admin", path="/offline-read")
    _, factory, provider, _, _ = environment(handler=handler)
    case = case_for("admin", path="/offline-update")
    case["method"] = "POST"
    case["request_builder"] = builder_config()
    case["effect"] = {
        "target": {"fields": ["usr_display_bil"]},
        "capture": {
            "enabled": True, "fields": {"usr_display_bil": "$.data.usr_display_bil"},
            "before": {"endpoint": "OFFLINE_READ"}, "after": {"endpoint": "OFFLINE_READ"},
        },
    }
    original = deepcopy(case)
    CaseRunner.run(case, factory, provider, apply_metadata=False)
    effect = Context.get_effect(case["id"])
    journal = RecoveryJournal(tmp_path / "recovery")
    path = journal.create(case["id"], "offline-target-user", effect["restore_payload"])
    journal_text = path.read_text(encoding="utf-8")
    try:
        assert effect["state"] == Context.EFFECT_APPLIED
        assert len(effect["restore_payload"]["payload"]) == 27
        assert not contains_credential(Context.all())
        assert not contains_credential(effect)
        assert Context.all()["variables"] == {}
        assert case == original
        for secret in (ADMIN_USER, ADMIN_PASSWORD, ADMIN_COOKIE):
            assert secret not in repr(Context.all())
            assert secret not in journal_text
            assert secret not in repr(evidence)
    finally:
        journal.finish(success=True)
    assert not journal.has_pending()


@pytest.mark.parametrize("response_kind", ["plain", "json"])
def test_allure_evidence_never_contains_login_or_echoed_authentication_canaries(evidence, response_kind):
    echo = f"ordinary response echo {ADMIN_COOKIE} {AUTHORIZATION} {AUTHORIZATION.split(' ', 1)[1]}"

    def handler(identity, request):
        return 200, {}, echo if response_kind == "plain" else {"code": 200, "message": echo}

    login_echo = {"message": f"{ADMIN_USER} {ADMIN_PASSWORD} {EncodeUtil.base64_encode(ADMIN_PASSWORD)} {ADMIN_COOKIE} {AUTHORIZATION}"}
    _, factory, provider, adapters, _ = environment(
        defaults={"Authorization": AUTHORIZATION}, handler=handler, login_payload=login_echo,
    )
    client = factory("Host-be", "admin")
    CaseRunner.run(case_for("admin"), factory, provider, apply_metadata=False)

    assert adapters[client].requests[-1].headers["Authorization"] == AUTHORIZATION
    assert len(login_requests(adapters[client])) == 1
    for secret in (ADMIN_USER, ADMIN_PASSWORD, EncodeUtil.base64_encode(ADMIN_PASSWORD), ADMIN_COOKIE, AUTHORIZATION, AUTHORIZATION.split(" ", 1)[1]):
        assert secret not in repr(evidence)
        assert secret not in repr(Context.all())


@pytest.mark.parametrize("response_kind", ["plain", "json"])
def test_capture_evidence_and_parse_errors_do_not_echo_authentication_material(monkeypatch, evidence, response_kind):
    echo = f"ordinary capture echo {LEARNER_COOKIE} {AUTHORIZATION} {AUTHORIZATION.split(' ', 1)[1]}"

    def handler(identity, request):
        return 200, {}, echo if response_kind == "plain" else {"code": 200, "message": echo}

    configure_capture(monkeypatch)
    _, factory, provider, adapters, _ = environment(defaults={"Authorization": AUTHORIZATION}, handler=handler)
    learner = factory("Host-be", "learner")
    args = (case_for("none"), {"endpoint": "OFFLINE_READ"}, "before", factory, provider, CaseRunner.build_header)
    if response_kind == "plain":
        with pytest.raises(ValueError) as raised:
            EffectExecutor.execute_capture(*args)
        error_text = str(raised.value)
    else:
        result = EffectExecutor.execute_capture(*args)
        assert result == {"code": 200, "message": echo}
        error_text = ""

    assert len(login_requests(adapters[learner])) == 1
    for secret in (LEARNER_USER, LEARNER_PASSWORD, EncodeUtil.base64_encode(LEARNER_PASSWORD), LEARNER_COOKIE, AUTHORIZATION, AUTHORIZATION.split(" ", 1)[1]):
        assert secret not in repr(evidence)
        assert secret not in error_text
        assert secret not in repr(Context.all())


@pytest.mark.parametrize("principal", ["admin", "learner"])
@pytest.mark.parametrize("entry", ["case", "capture"])
def test_business_transport_error_keeps_failure_but_never_echoes_known_credential_material(monkeypatch, evidence, principal, entry):
    cookie_value = ADMIN_COOKIE if principal == "admin" else LEARNER_COOKIE

    def handler(identity, request):
        raise requests.ConnectionError(f"offline business failure {cookie_value} {AUTHORIZATION}")

    _, factory, provider, adapters, _ = environment(defaults={"Authorization": AUTHORIZATION}, handler=handler)
    client = factory("Host-be", principal)
    if entry == "capture":
        configure_capture(monkeypatch, principal=principal)
    with pytest.raises(requests.ConnectionError) as raised:
        if entry == "case":
            CaseRunner.run(case_for(principal), factory, provider, apply_metadata=False)
        else:
            EffectExecutor.execute_capture(case_for("none"), {"endpoint": "OFFLINE_READ"}, "before", factory, provider, CaseRunner.build_header)

    assert len(login_requests(adapters[client])) == 1
    assert "offline business failure" in str(raised.value)
    assert provider._cache[("Host-be", principal)].cookie
    for secret in (cookie_value, AUTHORIZATION, AUTHORIZATION.split(" ", 1)[1]):
        assert secret not in str(raised.value)
        assert secret not in repr(evidence)
        assert secret not in repr(Context.all())


def test_known_credential_dictionary_keys_are_redacted_without_mutating_business_payload():
    credential = Credential("Host-be", "admin", "SESSION=DIAGNOSTIC_DICTIONARY_KEY_SECRET")
    payload = {
        credential.cookie: "ordinary message",
        "nested": [{"prefix-" + credential.cookie: "ordinary nested message"}],
        "ordinary_count": 42,
    }
    original = deepcopy(payload)
    diagnostic = redact_auth_evidence(payload, {credential.cookie})

    assert credential.cookie not in repr(diagnostic)
    assert diagnostic["ordinary_count"] == 42
    assert payload == original
    assert credential.cookie in payload
    assert payload[credential.cookie] == "ordinary message"


def test_exception_causes_and_contexts_with_custom_string_values_cannot_leak_in_formatted_traceback():
    secret = "DIAGNOSTIC_EXCEPTION_CHAIN_SECRET"

    class SecretStringError(Exception):
        def __init__(self):
            self.detail = secret
            super().__init__("ordinary inner arguments")

        def __str__(self):
            return "custom inner diagnostic " + self.detail

    for relation in ("__cause__", "__context__"):
        for inner in (OSError(5, secret), SecretStringError()):
            outer = RuntimeError("ordinary capture failure")
            setattr(outer, relation, inner)
            safe = sanitize_auth_exception(outer, {secret})
            formatted = "".join(traceback.format_exception(type(safe), safe, safe.__traceback__))
            assert secret not in str(safe)
            assert secret not in formatted

    harmless = RuntimeError("ordinary failure without credentials")
    assert sanitize_auth_exception(harmless, {secret}) is harmless
    assert type(harmless) is RuntimeError


@pytest.mark.parametrize("stage", ["transport", "capture_parse"])
def test_cookie_added_to_owned_session_during_failed_request_is_redacted_from_errors_and_evidence(monkeypatch, evidence, stage):
    rotated_secret = "DIAGNOSTIC_ROTATED_SESSION_SECRET"
    _, factory, provider, adapters, _ = environment()
    learner = factory("Host-be", "learner")
    admin = factory("Host-be", "admin")
    configure_capture(monkeypatch)
    adapter = adapters[learner]
    original_handler = adapter.handler

    def handler(request):
        if urlsplit(request.url).path == "/app/user/login/v3":
            return original_handler(request)
        learner.session.cookies.set("SESSION", rotated_secret, domain="be.example.invalid", path="/")
        if stage == "transport":
            raise requests.ConnectionError("ordinary offline failure " + rotated_secret)
        return 200, {}, "ordinary unparseable response " + rotated_secret

    adapter.handler = handler
    expected_error = requests.ConnectionError if stage == "transport" else ValueError
    with pytest.raises(expected_error) as raised:
        if stage == "transport":
            CaseRunner.run(case_for("learner"), factory, provider, apply_metadata=False)
        else:
            EffectExecutor.execute_capture(case_for("none"), {"endpoint": "OFFLINE_READ"}, "before", factory, provider, CaseRunner.build_header)

    assert len(login_requests(adapter)) == 1
    assert learner.session.cookies.get("SESSION") == rotated_secret
    assert rotated_secret not in str(raised.value)
    assert rotated_secret not in "".join(traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__))
    assert rotated_secret not in repr(evidence)
    assert rotated_secret not in repr(Context.all())
    assert admin.session.cookies.get_dict() == {}
    assert adapters[admin].requests == []
