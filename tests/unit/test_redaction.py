from wizbank_api_test.utils.redaction import SensitiveDataRedactor


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
