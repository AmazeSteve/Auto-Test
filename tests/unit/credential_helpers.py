"""Typed credentials and exact client bindings for offline Runtime tests."""

from wizbank_api_test.services.credential_provider import Credential


class FakeCredentialProvider:
    """No login transport; every configured identity owns one known Session."""

    def __init__(self, clients, *, cookie="SESSION=OFFLINE", credentials=None):
        self.clients = dict(clients)
        self.sessions = {key: client.session for key, client in self.clients.items()}
        if len({id(client) for client in self.clients.values()}) != len(self.clients):
            raise ValueError("Offline authenticated identities require distinct clients")
        if len({id(session) for session in self.sessions.values()}) != len(self.sessions):
            raise ValueError("Offline authenticated identities require distinct Sessions")
        self.credentials = (
            dict(credentials) if credentials is not None else {
                key: Credential(key[0], key[1], cookie) for key in self.clients
            }
        )
        self.get_calls = []
        self.match_calls = []

    def get(self, host_key, principal):
        key = (host_key, principal)
        self.get_calls.append(key)
        if principal not in ("admin", "learner") or key not in self.credentials:
            raise ValueError("No offline credential configured for requested identity")
        return self.credentials[key]

    def assert_client_matches(self, client, host_key, principal):
        key = (host_key, principal)
        self.match_calls.append(key)
        if (
            client is not self.clients.get(key)
            or getattr(client, "host_key", None) != host_key
            or getattr(client, "principal", None) != principal
            or client.session is not self.sessions.get(key)
        ):
            raise ValueError("Offline client identity does not match credential identity")
