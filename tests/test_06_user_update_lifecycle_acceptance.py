"""C1 real happy-path acceptance: all four operator gates are mandatory."""

import pytest

from wizbank_api_test.services.user_update_acceptance import (
    C1SafetyError, ROOT_CASE_ID, accept_lifecycle, load_cases, require_real_gates, safe_evidence,
)


def test_user_update_lifecycle_acceptance(request, monkeypatch):
    try:
        require_real_gates(request)
    except C1SafetyError as exc:
        pytest.skip(str(exc))
    cases = load_cases()
    with safe_evidence(monkeypatch):
        factory = request.getfixturevalue("api_client_factory")
        cookie = request.getfixturevalue("admin_cookie")
        accept_lifecycle(cases[ROOT_CASE_ID], cases, factory, cookie, request, monkeypatch)
