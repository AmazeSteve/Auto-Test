import pytest

from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_restore import EffectRestoreResolver


@pytest.fixture(autouse=True)
def reset_context():
    Context.clear()
    yield
    Context.clear()


def test_restore_mapping_reads_before_snapshot_explicitly():
    Context.mark_effect(
        case_id="WRITE_001",
        state=Context.EFFECT_APPLIED,
        snapshot={
            "before": {"usr_display_bil": "before-name"},
            "after": {"usr_display_bil": "after-name"},
            "diff": {
                "usr_display_bil": {
                    "from": "before-name",
                    "to": "after-name",
                }
            },
        },
    )

    case = {
        "id": "ROLLBACK_001",
        "json": {"usr_ent_id": 1830},
        "restore_mapping": {
            "source_effect": "WRITE_001",
            "fields": {
                "json.usr_display_bil": "before.usr_display_bil",
            },
        },
    }

    runtime_case, evidence = EffectRestoreResolver.apply(case)

    assert "usr_display_bil" not in case["json"]
    assert runtime_case["json"]["usr_display_bil"] == "before-name"
    assert evidence["json.usr_display_bil"]["value"] == "before-name"


def test_restore_mapping_missing_source_fails():
    case = {
        "id": "ROLLBACK_001",
        "json": {},
        "restore_mapping": {
            "source_effect": "WRITE_404",
            "fields": {
                "json.usr_display_bil": "before.usr_display_bil",
            },
        },
    }

    with pytest.raises(ValueError, match="找不到 Effect"):
        EffectRestoreResolver.apply(case)


def test_unknown_effect_keeps_logical_before_for_cleanup():
    from wizbank_api_test.core.case_runner import CaseRunner

    destructive_case = {
        "id": "WRITE_001",
        "effect": {
            "target": {
                "fields": ["usr_display_bil"],
            },
            "capture": {
                "fields": {
                    "usr_display_bil": "$.data.usr_display_bil",
                },
            },
        },
    }

    CaseRunner._mark_effect_unknown(
        case=destructive_case,
        before={
            "code": 200,
            "data": {"usr_display_bil": "before-name"},
        },
        error=RuntimeError("simulated transport failure"),
    )

    effect = Context.get_effect("WRITE_001")
    assert effect["state"] == Context.EFFECT_UNKNOWN
    assert effect["snapshot"]["before"] == {
        "usr_display_bil": "before-name"
    }

    rollback_case = {
        "id": "ROLLBACK_001",
        "json": {},
        "restore_mapping": {
            "source_effect": "WRITE_001",
            "fields": {
                "json.usr_display_bil": "before.usr_display_bil",
            },
        },
    }

    runtime_case, _ = EffectRestoreResolver.apply(rollback_case)
    assert runtime_case["json"]["usr_display_bil"] == "before-name"
