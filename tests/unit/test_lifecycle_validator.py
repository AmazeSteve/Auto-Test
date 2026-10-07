from wizbank_api_test.core.case_validator import CaseValidator


def _valid_cases():
    return [
        {
            "id": "WRITE_001",
            "tags": ["destructive", "update"],
            "effect": {"type": "update"},
            "lifecycle": {
                "verify": "VERIFY_001",
                "rollback": "ROLLBACK_001",
                "verify_rollback": "VERIFY_ROLLBACK_001",
            },
        },
        {
            "id": "VERIFY_001",
            "tags": ["destructive", "verify"],
            "depends_on": ["WRITE_001"],
        },
        {
            "id": "ROLLBACK_001",
            "tags": ["destructive", "rollback"],
            "destructive": True,
            "depends_on": ["WRITE_001"],
            "restore_mapping": {
                "source_effect": "WRITE_001",
                "fields": {
                    "json.name": "before.name",
                },
            },
        },
        {
            "id": "VERIFY_ROLLBACK_001",
            "tags": ["destructive", "rollback", "verify"],
            "depends_on": ["ROLLBACK_001"],
        },
    ]


def test_validator_accepts_explicit_lifecycle_contract():
    errors = CaseValidator._validate_lifecycle_contract(_valid_cases())
    assert errors == []


def test_validator_rejects_missing_lifecycle_child():
    cases = _valid_cases()
    cases[0]["lifecycle"]["verify"] = "MISSING_VERIFY"

    errors = CaseValidator._validate_lifecycle_contract(cases)

    assert any("MISSING_VERIFY" in error for error in errors)
