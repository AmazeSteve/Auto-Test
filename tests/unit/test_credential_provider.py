"""Real Requests preparation and cookie extraction, with offline login transport."""

import json
from collections import deque
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from email.message import Message
from http.cookiejar import DefaultCookiePolicy
from types import SimpleNamespace

import allure
import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter

from wizbank_api_test.core.client import ApiClient
from wizbank_api_test.core.config import Config
from wizbank_api_test.core.context import Context
from wizbank_api_test.services import credential_provider as module
from wizbank_api_test.services.credential_provider import Credential, CredentialProvider
from wizbank_api_test.tests import conftest as fixtures


COOKIE_SECRET = "SYNTHETIC_PROVIDER_COOKIE_SECRET"
ERROR_SECRET = "SYNTHETIC_PROVIDER_ERROR_SECRET"
PASSWORD = "SYNTHETIC_PROVIDER_PASSWORD"


class OfflineLoginAdapter(BaseAdapter):
    def __init__(self, client, outcomes):
        self.client = client
        self.outcomes = deque(outcomes)
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        outcome = self.outcomes.popleft()
        # Simulate an intermediate cookie change, including before transport failure.
        self.client.session.cookies.set(
            "INTERMEDIATE", "SHOULD_NOT_SURVIVE_FAILURE", domain="be.example.invalid", path="/"
        )
        if isinstance(outcome, Exception):
            raise outcome
        status, cookie = outcome
        response = requests.Response()
        response.status_code = status
        response.request = request
        response.url = request.url
        response._content = b'{"code":200}'
        response.encoding = "utf-8"
        message = Message()
        if cookie is not None:
            response.headers["Set-Cookie"] = cookie
            message["Set-Cookie"] = cookie
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
        return response

    def close(self):
        pass


def synthetic_config(*, legacy=True, foreign=True):
    config = Config.__new__(Config)
    config.data = {
        "base_urls": {
            "Host-be": "https://be.example.invalid",
            "Host-226": "https://foreign.example.invalid",
        },
        "default_headers": {"Accept": "application/json"},
        "request": {"verify_ssl": True, "timeout": 1},
    }
    if legacy:
        config.data["users"] = {
            principal: {"username": f"legacy-{principal}", "password": PASSWORD}
            for principal in ("admin", "learner")
        }
    if foreign:
        config.data["credentials"] = {
            "Host-226": {
                principal: {"username": f"foreign-{principal}", "password": PASSWORD}
                for principal in ("admin", "learner")
            }
        }
    return config


def environment(config=None):
    config = config or synthetic_config()
    factory = fixtures.api_client_factory.__wrapped__(config)
    provider = CredentialProvider(factory, config)
    return config, factory, provider


def mount(client, outcomes=None):
    adapter = OfflineLoginAdapter(client, outcomes or [(200, f"SESSION={COOKIE_SECRET}; Path=/; HttpOnly")])
    client.session.mount("https://", adapter)
    return adapter


def seed(client):
    jar = client.session.cookies
    jar.set_policy(DefaultCookiePolicy(blocked_domains=["blocked.example.invalid"]))
    jar.set("SAME", "ROOT", domain="be.example.invalid", path="/", secure=True,
            expires=2_000_000_000, rest={"HttpOnly": True, "SameSite": "Strict"})
    jar.set("SAME", "PATH", domain="be.example.invalid", path="/nested")
    jar.set("OTHER", "OTHER_HOST", domain="foreign.example.invalid", path="/")
    return jar, deepcopy([vars(cookie) for cookie in jar]), deepcopy(vars(jar.get_policy()))


def assert_restored(client, state):
    jar, cookies, policy = state
    assert client.session.cookies is jar
    assert [vars(cookie) for cookie in jar] == cookies
    assert vars(jar.get_policy()) == policy


@pytest.fixture(autouse=True)
def offline(monkeypatch, bind_context):
    Context.clear()
    evidence = []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: evidence.append(str(body)))
    monkeypatch.setattr(
        HTTPAdapter, "send", lambda *args, **kwargs: pytest.fail("Real HTTP is forbidden")
    )
    yield evidence
    assert Context.all() == {"variables": {}, "cases": {}, "effects": {}}
    for secret in (COOKIE_SECRET, ERROR_SECRET, PASSWORD):
        assert secret not in repr(Context.all())
        assert secret not in repr(evidence)
    Context.clear()


def test_credential_has_exact_three_fields_is_immutable_and_hides_cookie():
    credential = Credential("Host-be", "admin", COOKIE_SECRET)
    assert [item.name for item in fields(credential)] == ["host_key", "principal", "cookie"]
    assert COOKIE_SECRET not in repr(credential)
    assert credential.cookie == COOKIE_SECRET
    credential.assert_matches("Host-be", "admin")
    with pytest.raises(FrozenInstanceError):
        credential.cookie = "replacement"


@pytest.mark.parametrize("cookie", [None, "", " ", 0, {}, []])
def test_credential_rejects_invalid_cookie_without_echoing_input(cookie):
    with pytest.raises(ValueError, match="non-empty Cookie") as raised:
        Credential("Host-be", "admin", cookie)
    assert COOKIE_SECRET not in str(raised.value)


@pytest.mark.parametrize("host,principal", [
    ("Host-226", "admin"), ("Host-be", "learner"), ("Host-be", "none"),
])
def test_credential_identity_mismatch_is_secret_safe(host, principal):
    credential = Credential("Host-be", "admin", COOKIE_SECRET)
    with pytest.raises(ValueError) as raised:
        credential.assert_matches(host, principal)
    assert COOKIE_SECRET not in str(raised.value)


@pytest.mark.parametrize("host,principal", [("", "admin"), (None, "admin"), ("Host-be", "none"), ("Host-be", None)])
def test_credential_rejects_non_authenticated_identity(host, principal):
    with pytest.raises(ValueError, match="Invalid credential identity"):
        Credential(host, principal, COOKIE_SECRET)


def test_provider_construction_is_lazy_and_does_not_even_request_a_client():
    calls = []
    provider = CredentialProvider(lambda *args, **kwargs: calls.append((args, kwargs)), object())
    assert calls == []
    assert provider._cache == {}
    assert provider._clients == {}
    assert provider._sessions == {}


@pytest.mark.parametrize("principal", ["none", None, "root", "", 42])
def test_provider_rejects_none_and_unsupported_principals_before_factory(principal):
    calls = []
    provider = CredentialProvider(lambda *args, **kwargs: calls.append((args, kwargs)), object())
    with pytest.raises(ValueError, match="Invalid credential identity"):
        provider.get("Host-be", principal)
    assert calls == []
    assert provider._cache == {}


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_host_be_legacy_credentials_login_once_and_cache_exact_successful_identity(principal):
    config, factory, provider = environment()
    client = factory("Host-be", principal)
    adapter = mount(client)
    original_config = deepcopy(config.data)
    config_calls = []
    get_user = config.get_credential_user
    config.get_credential_user = lambda h, p: (config_calls.append((h, p)), get_user(h, p))[1]

    provider.assert_client_matches(client, "Host-be", principal)
    assert adapter.requests == []
    assert config_calls == []
    credential = provider.get("Host-be", principal)
    cached = provider.get("Host-be", principal)

    assert cached is credential
    assert credential.host_key == "Host-be"
    assert credential.principal == principal
    assert credential.cookie == f"SESSION={COOKIE_SECRET}; Path=/; HttpOnly"
    assert client.session.cookies.get("SESSION") == COOKIE_SECRET
    assert len(adapter.requests) == 1
    assert json.loads(adapter.requests[0].body)["usrSteUsrId"] == f"legacy-{principal}"
    assert config_calls == [("Host-be", principal)]
    assert config.data == original_config


def test_distinct_host_and_principal_keys_never_share_credentials_or_sessions():
    _, factory, provider = environment()
    keys = [("Host-be", "admin"), ("Host-be", "learner"), ("Host-226", "admin"), ("Host-226", "learner")]
    clients = [factory(*key) for key in keys]
    adapters = [mount(client) for client in clients]

    credentials = [provider.get(*key) for key in keys]

    assert len({id(item) for item in credentials}) == 4
    assert len({id(item.session) for item in clients}) == 4
    assert credentials[0] != credentials[1]
    assert len(provider._cache) == 4
    assert all(len(adapter.requests) == 1 for adapter in adapters)
    for key, client, credential, adapter in zip(keys, clients, credentials, adapters):
        assert provider.get(*key) is credential
        assert provider._clients[key] is client
        assert provider._sessions[key] is client.session
        username = "legacy" if key[0] == "Host-be" else "foreign"
        assert json.loads(adapter.requests[0].body)["usrSteUsrId"] == f"{username}-{key[1]}"


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_foreign_host_with_only_legacy_users_fails_before_transport_and_cache(principal):
    _, factory, provider = environment(synthetic_config(foreign=False))
    client = factory("Host-226", principal)
    adapter = mount(client)
    state = seed(client)

    with pytest.raises(ValueError, match="Credential acquisition failed"):
        provider.get("Host-226", principal)

    assert adapter.requests == []
    assert provider._cache == {}
    assert provider._clients == {}
    assert_restored(client, state)


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_missing_host_be_config_has_no_transport_or_cache(principal):
    _, factory, provider = environment(synthetic_config(legacy=False))
    client = factory("Host-be", principal)
    adapter = mount(client)
    state = seed(client)

    with pytest.raises(ValueError):
        provider.get("Host-be", principal)

    assert adapter.requests == []
    assert provider._cache == {}
    assert_restored(client, state)


@pytest.mark.parametrize("principal", ["admin", "learner"])
@pytest.mark.parametrize("failure", ["transport", "status", "missing_cookie"])
def test_login_failure_restores_complete_owned_jar_without_touching_other_sessions(principal, failure):
    _, factory, provider = environment()
    keys = [("Host-be", "admin"), ("Host-be", "learner"), ("Host-226", "admin")]
    states = {key: seed(factory(*key)) for key in keys}
    client = factory("Host-be", principal)
    outcomes = {
        "transport": requests.ConnectionError(ERROR_SECRET),
        "status": (403, f"SESSION={COOKIE_SECRET}; Path=/"),
        "missing_cookie": (200, None),
    }
    adapter = mount(client, [outcomes[failure]])
    client.session.proxies = {"https": "http://proxy.example.invalid"}
    original_proxies = deepcopy(client.session.proxies)

    with pytest.raises(ValueError, match="Credential acquisition failed") as raised:
        provider.get("Host-be", principal)

    assert len(adapter.requests) == 1
    assert raised.value.__suppress_context__ is True
    assert raised.value.__cause__ is None
    assert ERROR_SECRET not in str(raised.value)
    assert COOKIE_SECRET not in str(raised.value)
    assert provider._cache == {}
    assert provider._clients == {}
    assert client.session.trust_env is True
    assert client.session.proxies == original_proxies
    for key, state in states.items():
        assert_restored(factory(*key), state)


@pytest.mark.parametrize("failure", ["config", "service_construction", "credential_construction", "post_validation"])
def test_failure_at_any_login_validation_stage_restores_jar_and_leaves_no_partial_cache(monkeypatch, failure):
    config, factory, provider = environment()
    client = factory("Host-be", "admin")
    state = seed(client)
    adapter = mount(client)

    def fail(*args, **kwargs):
        client.session.cookies.set("FAILED", ERROR_SECRET)
        client.session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=["bad.example.invalid"]))
        raise ValueError(ERROR_SECRET)

    if failure == "config":
        monkeypatch.setattr(config, "get_credential_user", fail)
    elif failure == "service_construction":
        monkeypatch.setattr(module, "AuthService", fail)
    elif failure == "credential_construction":
        monkeypatch.setattr(module, "Credential", fail)
    else:
        monkeypatch.setattr(Credential, "assert_matches", fail)

    with pytest.raises(ValueError, match="Credential acquisition failed") as raised:
        provider.get("Host-be", "admin")

    assert ERROR_SECRET not in str(raised.value)
    assert provider._cache == {}
    assert provider._clients == {}
    assert len(adapter.requests) == int(failure in ("credential_construction", "post_validation"))
    assert_restored(client, state)


def test_replaced_jar_during_failed_login_restores_original_jar_object(monkeypatch):
    config, factory, provider = environment()
    client = factory("Host-be", "admin")
    state = seed(client)
    adapter = mount(client)

    def fail(*args, **kwargs):
        client.session.cookies = requests.cookies.RequestsCookieJar()
        client.session.cookies.set("FAILED", ERROR_SECRET)
        raise ValueError(ERROR_SECRET)

    monkeypatch.setattr(config, "get_credential_user", fail)
    with pytest.raises(ValueError):
        provider.get("Host-be", "admin")

    assert adapter.requests == []
    assert provider._cache == {}
    assert_restored(client, state)


@pytest.mark.parametrize("principal", ["admin", "learner"])
def test_failed_attempt_can_retry_from_clean_same_session_and_then_cache(principal):
    _, factory, provider = environment()
    client = factory("Host-be", principal)
    state = seed(client)
    adapter = mount(client, [(401, "FAILED=NEVER_REUSED; Path=/"), (200, f"SESSION={COOKIE_SECRET}; Path=/")])

    with pytest.raises(ValueError):
        provider.get("Host-be", principal)
    assert_restored(client, state)
    credential = provider.get("Host-be", principal)

    assert "FAILED=" not in adapter.requests[1].headers.get("Cookie", "")
    assert "INTERMEDIATE=" not in adapter.requests[1].headers.get("Cookie", "")
    assert credential.cookie == f"SESSION={COOKIE_SECRET}; Path=/"
    assert provider.get("Host-be", principal) is credential
    assert len(adapter.requests) == 2
    assert client.session.cookies is state[0]


@pytest.mark.parametrize("attribute,value", [("host_key", "Host-226"), ("principal", "learner")])
def test_mismatched_factory_identity_fails_before_login_transport(attribute, value):
    _, factory, _ = environment()
    client = factory("Host-be", "admin")
    adapter = mount(client)
    setattr(client, attribute, value)
    provider = CredentialProvider(lambda host, principal: client, object())

    with pytest.raises(ValueError):
        provider.get("Host-be", "admin")

    assert adapter.requests == []
    assert provider._cache == {}


def test_runtime_client_must_be_exact_provider_owned_client_before_any_login():
    _, factory, provider = environment()
    owned = factory("Host-be", "admin")
    other = ApiClient(owned.base_url, principal="admin", host_key="Host-be")
    adapter = mount(owned)

    with pytest.raises(ValueError, match="Credential client identity mismatch"):
        provider.assert_client_matches(other, "Host-be", "admin")

    assert adapter.requests == []
    assert provider._cache == {}
    provider.assert_client_matches(owned, "Host-be", "admin")
    assert adapter.requests == []


def test_cached_credential_rejects_replacement_client_instead_of_switching_session():
    config, factory, _ = environment()
    first = factory("Host-be", "admin")
    replacement = ApiClient(first.base_url, principal="admin", host_key="Host-be")
    selected = [first]
    provider = CredentialProvider(lambda host, principal: selected[0], config)
    first_adapter, replacement_adapter = mount(first), mount(replacement)
    credential = provider.get("Host-be", "admin")
    selected[0] = replacement

    with pytest.raises(ValueError):
        provider.get("Host-be", "admin")
    with pytest.raises(ValueError):
        provider.assert_client_matches(replacement, "Host-be", "admin")

    assert len(first_adapter.requests) == 1
    assert replacement_adapter.requests == []
    assert provider._cache[("Host-be", "admin")] is credential
    assert provider._clients[("Host-be", "admin")] is first


def test_factory_change_after_login_is_rejected_and_cookie_jar_restored(monkeypatch):
    config, factory, _ = environment()
    first = factory("Host-be", "admin")
    replacement = ApiClient(first.base_url, principal="admin", host_key="Host-be")
    selected = [first]
    provider = CredentialProvider(lambda host, principal: selected[0], config)
    state = seed(first)
    adapter = mount(first)
    original_post = first.post

    def post(*args, **kwargs):
        response = original_post(*args, **kwargs)
        selected[0] = replacement
        return response

    monkeypatch.setattr(first, "post", post)
    with pytest.raises(ValueError):
        provider.get("Host-be", "admin")

    assert len(adapter.requests) == 1
    assert provider._cache == {}
    assert provider._clients == {}
    assert_restored(first, state)


def test_cached_credential_rejects_replaced_session_without_mutating_either_jar():
    _, factory, provider = environment()
    admin = factory("Host-be", "admin")
    learner = factory("Host-be", "learner")
    learner_state = seed(learner)
    adapter = mount(admin)
    credential = provider.get("Host-be", "admin")
    original_session = admin.session
    admin_state = seed(admin)
    admin.session = learner.session

    with pytest.raises(ValueError):
        provider.get("Host-be", "admin")
    with pytest.raises(ValueError):
        provider.assert_client_matches(admin, "Host-be", "admin")

    assert len(adapter.requests) == 1
    assert provider._cache[("Host-be", "admin")] is credential
    assert provider._sessions[("Host-be", "admin")] is original_session
    assert_restored(learner, learner_state)
    admin.session = original_session
    assert_restored(admin, admin_state)


@pytest.mark.parametrize("stage", ["config_failure", "after_login"])
def test_session_replacement_during_login_restores_original_without_touching_other_principal(monkeypatch, stage):
    config, factory, provider = environment()
    admin = factory("Host-be", "admin")
    learner = factory("Host-be", "learner")
    original_session = admin.session
    admin_state, learner_state = seed(admin), seed(learner)
    adapter = mount(admin)

    if stage == "config_failure":
        def replace_and_fail(*args, **kwargs):
            admin.session = learner.session
            raise ValueError(ERROR_SECRET)

        monkeypatch.setattr(config, "get_credential_user", replace_and_fail)
    else:
        original_post = admin.post

        def replace_after_post(*args, **kwargs):
            response = original_post(*args, **kwargs)
            admin.session = learner.session
            return response

        monkeypatch.setattr(admin, "post", replace_after_post)

    with pytest.raises(ValueError, match="Credential acquisition failed") as raised:
        provider.get("Host-be", "admin")

    assert ERROR_SECRET not in str(raised.value)
    assert admin.session is original_session
    assert len(adapter.requests) == int(stage == "after_login")
    assert provider._cache == {}
    assert provider._clients == {}
    assert provider._sessions == {}
    assert_restored(admin, admin_state)
    assert_restored(learner, learner_state)
