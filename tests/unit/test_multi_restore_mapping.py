from wizbank_api_test.core.case_validator import CaseValidator
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_restore import EffectRestoreResolver


def test_restore_mapping_supports_multiple_fields():
    Context.clear()
    Context.mark_effect(
        "WRITE_001",
        state=Context.EFFECT_APPLIED,
        snapshot={
            "before": {
                "usr_display_bil": "Original",
                "userGroupId": "GROUP-1",
                "gradeId": "GRADE-1",
            },
            "after": {},
            "diff": {},
        },
    )

    case = {
        "id": "ROLLBACK_001",
        "json": {"usr_ent_id": "1830"},
        "restore_mapping": {
            "source_effect": "WRITE_001",
            "fields": {
                "json.usr_display_bil": "before.usr_display_bil",
                "json.userGroupId": "before.userGroupId",
                "json.gradeId": "before.gradeId",
            },
        },
    }

    runtime_case, evidence = EffectRestoreResolver.apply(case)

    assert runtime_case["json"]["usr_display_bil"] == "Original"
    assert runtime_case["json"]["userGroupId"] == "GROUP-1"
    assert runtime_case["json"]["gradeId"] == "GRADE-1"
    assert set(evidence) == {
        "json.usr_display_bil",
        "json.userGroupId",
        "json.gradeId",
    }


def test_validator_accepts_multiple_restore_fields():
    cases = [
        {
            "id": "WRITE_001",
            "effect": {
                "target": {
                    "fields": [
                        "usr_display_bil",
                        "userGroupId",
                        "gradeId",
                    ]
                },
                "capture": {
                    "fields": {
                        "usr_display_bil": "$.data.usr_display_bil",
                        "userGroupId": "$.data.userGroupId",
                        "gradeId": "$.data.gradeId",
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
                    "json.usr_display_bil": "before.usr_display_bil",
                    "json.userGroupId": "before.userGroupId",
                    "json.gradeId": "before.gradeId",
                },
            },
        },
    ]

    errors = CaseValidator._validate_restore_mapping_contract(cases)

    assert errors == []
