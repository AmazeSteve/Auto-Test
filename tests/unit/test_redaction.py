from wizbank_api_test.utils.redaction import SensitiveDataRedactor
from copy import deepcopy
import json

import pytest


def test_redaction_masks_cookie_and_context_cookie():
    payload = {
        "headers": {
            "Cookie": "SESSION=secret",
            "Content-Type": "application/json",
        },
        "admin_cookie": "SESSION=secret",
        "nested": {
            "access_token": "abc",
            "safe": "value",
        },
    }

    masked = SensitiveDataRedactor.redact(payload)

    assert masked["headers"]["Cookie"] == "<REDACTED>"
    assert masked["admin_cookie"] == "<REDACTED>"
    assert masked["nested"]["access_token"] == "<REDACTED>"
    assert masked["nested"]["safe"] == "value"
    assert payload["headers"]["Cookie"] == "SESSION=secret"


@pytest.mark.parametrize("key", [
    "usr_ste_usr_id", "usr_display_bil", "usr_nickname", "usr_bday",
    "usr_email", "usr_tel_1", "usr_fax_1", "usr_extra_2",
])
def test_user_pii_is_redacted_recursively_and_input_is_unchanged(key):
    value = {"json": {key: "private-user-value"}, "response": [{key.upper(): "private-user-value"}]}
    original = deepcopy(value)
    masked = SensitiveDataRedactor.redact(value)
    assert "private-user-value" not in str(masked)
    assert masked["json"][key] == SensitiveDataRedactor.REDACTED
    assert value == original


def test_full_restore_payload_is_metadata_only_in_both_context_attachments(monkeypatch):
    from wizbank_api_test.core.case_runner import CaseRunner
    from wizbank_api_test.core.context import Context
    Context.clear()
    payload = {"business_id": "private-business-id", "custom_field": ["private-custom-value"],
               "usr_email": "private-email"}
    Context.mark_effect("WRITE", restore_payload={"target": "json", "payload": payload})
    original = Context.all()
    attachments = {}
    monkeypatch.setattr("allure.attach", lambda body, name, **kw: attachments.setdefault(name, body))

    class FakeClient:
        @staticmethod
        def request(**kw):
            return object()

    try:
        CaseRunner.run({"id": "PLAIN", "method": "POST", "path": "/offline"}, lambda host: FakeClient(), "fake cookie")
        for name in ("context after effect", "runtime context snapshot"):
            assert name in attachments
            for raw in ("private-business-id", "private-custom-value", "private-email"):
                assert raw not in attachments[name]
            assert "field_count" in attachments[name] and "custom_field" in attachments[name]
        assert Context.get_effect("WRITE") == original["effects"]["WRITE"]
        summary = SensitiveDataRedactor.redact(Context.all())["effects"]["WRITE"]["restore_payload"]
        assert summary == {"target": "json", "field_count": 3, "keys": sorted(payload)}
    finally:
        Context.clear()


def test_api_client_request_evidence_redacts_pii_and_auth_without_changing_request(monkeypatch):
    from wizbank_api_test.core.client import ApiClient
    keys = ["usr_ste_usr_id", "usr_display_bil", "usr_nickname", "usr_bday",
            "usr_email", "usr_tel_1", "usr_fax_1", "usr_extra_2"]
    payload = {key: "private-" + key for key in keys}
    attachments = []
    calls = []
    monkeypatch.setattr("allure.attach", lambda body, **kw: attachments.append(body))
    client = ApiClient("https://example.invalid", verify_ssl=True)

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": deepcopy(payload)}

    def fake_request(**kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(client.session, "request", fake_request)
    client.post("/offline", json=payload, headers={"Cookie": "private-cookie"})
    assert calls[0]["json"] == payload
    assert calls[0]["headers"]["Cookie"] == "private-cookie"
    assert calls[0]["verify"] is True
    for raw in [*payload.values(), "private-cookie"]:
        assert all(raw not in evidence for evidence in attachments)


def test_restore_evidence_attachment_contains_keys_and_counts_only(monkeypatch):
    from wizbank_api_test.core.context import Context
    from wizbank_api_test.core.lifecycle_runner import LifecycleRunner
    attachments = []
    Context.clear()
    Context.mark_effect("WRITE", restore_payload={"target": "json", "payload": {"arbitrary": "private-value"}})
    monkeypatch.setattr("allure.attach", lambda body, **kw: attachments.append(body))
    try:
        runtime = LifecycleRunner._prepare_runtime_case({"restore_payload": {"source_effect": "WRITE"}})
        assert runtime["json"] == {"arbitrary": "private-value"}
        assert len(attachments) == 1
        evidence = json.loads(attachments[0])
        assert evidence == {"source_effect": "WRITE", "target": "json", "field_count": 1, "keys": ["arbitrary"]}
        assert "private-value" not in attachments[0]
    finally:
        Context.clear()
