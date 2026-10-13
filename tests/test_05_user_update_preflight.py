"""Operator-only read-only C1 preflight; Cloud validates collection/mocks only."""

import pytest

from wizbank_api_test.services.user_update_acceptance import (
    C1SafetyError, ROOT_CASE_ID, load_cases, preflight, require_username, safe_evidence,
)


def test_user_update_preflight(request, monkeypatch):
    try:
        require_username()
    except C1SafetyError as exc:
        pytest.skip(str(exc))
    cases = load_cases()
    # Fixtures that can log in are requested only AFTER the explicit target gate.
    with safe_evidence(monkeypatch):
        factory = request.getfixturevalue("api_client_factory")
        cookie = request.getfixturevalue("admin_cookie")
        preflight(cases[ROOT_CASE_ID], cases, factory, cookie, request)
