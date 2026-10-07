from wizbank_api_test.core.case_validator import CaseValidator


def test_validator_rejects_unknown_data_factory_key():
    cases = [
        {
            "id": "CASE_001",
            "json": {"name": "{{$data.unknown_name}}"},
        }
    ]

    errors = CaseValidator._validate_data_factory_references(cases)

    assert errors
    assert "unknown_name" in errors[0]


def test_validator_rejects_restore_field_not_declared_in_effect():
    cases = [
        {
            "id": "WRITE_001",
            "effect": {
                "target": {"fields": ["usr_display_bil"]},
                "capture": {
                    "fields": {
                        "usr_display_bil": "$.data.usr_display_bil",
                    }
                },
            },
        },
        {
            "id": "ROLLBACK_001",
            "tags": ["destructive", "rollback"],
            "destructive": True,
            "depends_on": ["WRITE_001"],
            "restore_mapping": {
                "source_effect": "WRITE_001",
                "fields": {
                    "json.usr_email": "before.usr_email",
                },
            },
        },
    ]

    errors = CaseValidator._validate_restore_mapping_contract(cases)

    assert any("usr_email" in error for error in errors)
