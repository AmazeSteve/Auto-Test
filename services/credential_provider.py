"""Lazy host/principal credentials bound to the existing owned HTTP Sessions."""

from copy import deepcopy
from dataclasses import dataclass, field

from wizbank_api_test.core.client import assert_client_identity, get_principal_client
from wizbank_api_test.services.auth_service import AuthService


def _credential_key(host_key, principal):
    if not isinstance(host_key, str) or not host_key.strip():
        raise ValueError("Invalid credential identity")
    if not isinstance(principal, str) or principal not in ("admin", "learner"):
        raise ValueError("Invalid credential identity")
    return host_key, principal


@dataclass(frozen=True)
class Credential:
    host_key: str
    principal: str
    cookie: str = field(repr=False)

    def __post_init__(self):
        _credential_key(self.host_key, self.principal)
        if not isinstance(self.cookie, str) or not self.cookie.strip():
            raise ValueError("Credential requires a non-empty Cookie")

    def assert_matches(self, host_key, principal):
        if _credential_key(host_key, principal) != (self.host_key, self.principal):
            raise ValueError("Credential identity mismatch")


class CredentialProvider:
    def __init__(self, api_client_factory, config):
        self._api_client_factory = api_client_factory
        self._config = config
        self._cache = {}
        self._clients = {}
        self._sessions = {}

    def _matching_client(self, host_key, principal):
        key = _credential_key(host_key, principal)
        client = get_principal_client(self._api_client_factory, host_key, principal)
        assert_client_identity(client, host_key, principal)
        if key in self._clients and self._clients[key] is not client:
            raise ValueError("Credential client identity mismatch")
        if key in self._sessions and self._sessions[key] is not client.session:
            raise ValueError("Credential Session identity mismatch")
        return client

    def assert_client_matches(self, api_client, host_key, principal):
        """Check actual client ownership before an authenticated Runtime lookup."""
        try:
            client = self._matching_client(host_key, principal)
            if client is not api_client:
                raise ValueError("Credential client identity mismatch")
        except Exception:
            raise ValueError("Credential client identity mismatch") from None

    def get(self, host_key, principal):
        key = _credential_key(host_key, principal)
        try:
            client = self._matching_client(host_key, principal)
            if key in self._cache:
                credential = self._cache[key]
                credential.assert_matches(host_key, principal)
                return credential
            session = client.session
            jar = session.cookies
            snapshot = deepcopy(jar)
        except Exception:
            raise ValueError("Credential acquisition failed") from None

        try:
            service = AuthService(
                client, self._config, host_key=host_key, principal=principal
            )
            cookie = service.admin_login() if principal == "admin" else service.learner_login()
            credential = Credential(host_key, principal, cookie)
            credential.assert_matches(host_key, principal)
            if client.session is not session:
                raise ValueError("Credential Session identity mismatch")
            self.assert_client_matches(client, host_key, principal)
        except Exception:
            # Keep the original jar object and its full cookie attributes/policy.
            try:
                client.session = session
                session.cookies = jar
                jar.clear()
                jar.update(snapshot)
                jar.set_policy(snapshot.get_policy())
            except Exception:
                raise ValueError("Credential cookie restoration failed") from None
            raise ValueError("Credential acquisition failed") from None

        self._cache[key] = credential
        self._clients[key] = client
        self._sessions[key] = session
        return credential
