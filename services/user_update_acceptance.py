"""C1 acceptance-only gates/observation; the B1/B2 runtime stays frozen."""

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path

import allure

from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.case_selector import CaseSelector
from wizbank_api_test.core.case_validator import CaseValidator
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner
from wizbank_api_test.services.request_payload_resolver import RequestPayloadResolver
from wizbank_api_test.services import user_update_state_validation as state
from wizbank_api_test.utils.recovery_journal import PendingRecoveryError, RecoveryJournal, sha256, validate_envelope


ROOT = Path(__file__).resolve().parents[1]
ROOT_CASE_ID = "WB_USER_DESTRUCTIVE_DEMO_001"
_EVIDENCE_NAMES = {
    "C1 preflight summary", "C1 mutation semantic diff",
    "C1 restored semantic diff", "C1 lifecycle summary",
}


class C1SafetyError(ValueError):
    pass


class C1AcceptanceError(AssertionError):
    pass


def require_username(environ=None):
    environ = os.environ if environ is None else environ
    username = environ.get("TEST_USERNAME")
    if not isinstance(username, str) or not username.strip() or username != username.strip():
        raise C1SafetyError("C1 requires explicitly set, nonblank TEST_USERNAME without surrounding whitespace")
    return username


def require_real_gates(request, environ=None):
    environ = os.environ if environ is None else environ
    if not request.config.getoption("--allow-destructive"):
        raise C1SafetyError("C1 requires --allow-destructive")
    if not request.config.getoption("--allow-c1-real-lifecycle"):
        raise C1SafetyError("C1 requires --allow-c1-real-lifecycle")
    username = require_username(environ)
    if environ.get("C1_CONFIRM_USERNAME") != username:
        raise C1SafetyError("C1 requires C1_CONFIRM_USERNAME to equal TEST_USERNAME exactly")
    return username


@contextmanager
def safe_evidence(monkeypatch):
    """Suppress legacy body attachments during C1 only, including lazy login."""
    original_attach = allure.attach
    original_step = allure.step
    with monkeypatch.context() as scoped:
        def attach(body, name=None, attachment_type=None, extension=None):
            if name in _EVIDENCE_NAMES:
                return original_attach(body, name=name, attachment_type=attachment_type, extension=extension)

        scoped.setattr(allure, "attach", attach)
        # Runtime step labels can embed business IDs in resolved endpoint paths.
        scoped.setattr(allure, "step", lambda *args, **kwargs: original_step("C1 runtime operation"))
        yield


def _attach(name, evidence):
    allure.attach(json.dumps(evidence, sort_keys=True, ensure_ascii=False), name=name,
                  attachment_type=allure.attachment_type.JSON)


def load_cases():
    cases = CaseLoader.load_cases(ROOT / "data/cases", ROOT / "data/endpoints")
    CaseValidator.validate_all(cases)
    return {case["id"]: case for case in cases}


def readonly_dependencies(root, case_map):
    ordered, visiting, visited = [], set(), set()

    def visit(case_id):
        if case_id in visiting:
            raise C1SafetyError("C1 dependency cycle")
        if case_id in visited:
            return
        case = case_map.get(case_id)
        if case is None:
            raise C1SafetyError("C1 dependency Case is missing")
        tags = case.get("tags") or []
        if ("readonly" not in tags or "destructive" in tags or case.get("destructive") is True
                or case.get("effect") or any(key in case for key in ("request_builder", "restore_payload", "restore_mapping", "lifecycle"))):
            raise C1SafetyError("C1 dependency chain must be entirely read-only")
        visiting.add(case_id)
        for dependency in CaseSelector.normalize_depends(case.get("depends_on")):
            visit(dependency)
        visiting.remove(case_id)
        visited.add(case_id)
        ordered.append(case)

    for dependency in CaseSelector.normalize_depends(root.get("depends_on")):
        visit(dependency)
    return ordered


def prepare_dependencies(root, case_map, api_client_factory, credential_provider, request, username):
    if require_username() != username:
        raise C1SafetyError("C1 target environment changed")
    dependencies = readonly_dependencies(root, case_map)
    Context.clear()
    for case in dependencies:
        LifecycleRunner.run_case(case, api_client_factory, credential_provider, request)


def _read_model(payload, username):
    model = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(model, dict) or model.get("usr_ste_usr_id") != username:
        raise C1SafetyError("C1 returned target identity does not match explicit TEST_USERNAME")
    return model


def validate_plan(root, before, result, username):
    model = _read_model(before, username)
    cur_lan = root["request_builder"]["arguments"]["cur_lan"]
    baseline = state.normalize(model, cur_lan)
    validate_envelope(result.restore_payload)
    if (result.target != "json" or result.builder_name != "user_update"
            or result.changed_fields != {"usr_display_bil"}
            or not isinstance(result.body, dict) or set(result.body) != set(baseline)
            or result.restore_payload["payload"] != baseline):
        raise C1SafetyError("C1 plan does not match the normalized 27-field baseline/mutation contract")
    state.assert_mutation_only(state.canonicalize(baseline), state.canonicalize(result.body))
    return {
        "case_id": root["id"], "target_user_hash": sha256(username), "field_count": len(baseline),
        "changed_fields": sorted(result.changed_fields), "baseline_sha256": sha256(baseline),
        "mutation_sha256": sha256(result.body), "restore_sha256": sha256(result.restore_payload),
        "READY": "YES",
    }


def preflight(root, case_map, api_client_factory, credential_provider, request):
    username = require_username()
    try:
        prepare_dependencies(root, case_map, api_client_factory, credential_provider, request, username)
        before = EffectExecutor.capture_before(case=root, api_client_factory=api_client_factory,
                                              credential_provider=credential_provider, header_builder=CaseRunner.build_header)
        _read_model(before, username)
        result = RequestPayloadResolver.resolve(root, before)
        summary = validate_plan(root, before, result, username)
        _attach("C1 preflight summary", summary)
        return summary
    except Exception as exc:
        _attach("C1 preflight summary", {"case_id": root["id"], "target_user_hash": sha256(username),
                "READY": "NO", "error_type": type(exc).__name__})
        raise C1AcceptanceError("C1 preflight failed before mutation; see safe summary") from None


def accept_lifecycle(root, case_map, api_client_factory, credential_provider, request, monkeypatch, journal=None):
    username = require_real_gates(request)
    journal = journal if journal is not None else RecoveryJournal()
    observations = {}
    summary = {"case_id": root["id"], "target_user_hash": sha256(username), "READY": "NO"}
    try:
        journal.assert_no_pending()
        prepare_dependencies(root, case_map, api_client_factory, credential_provider, request, username)
        capture_before, capture_after = EffectExecutor.capture_before, EffectExecutor.capture_after
        resolve = RequestPayloadResolver.resolve

        def before_observer(*args, **kwargs):
            payload = capture_before(*args, **kwargs)
            _read_model(payload, username)
            observations["before"] = deepcopy(payload)
            return payload

        def resolver_observer(case, before):
            result = resolve(case, before)
            if case["id"] != root["id"]:
                raise C1SafetyError("C1 observed an unexpected Builder Case")
            summary.update(validate_plan(case, before, result, username))
            journal.create(root["id"], username, result.restore_payload)
            observations["restore_payload"] = deepcopy(result.restore_payload)
            return result

        def after_observer(*args, **kwargs):
            payload = capture_after(*args, **kwargs)
            observations["mutation"] = deepcopy(payload)
            return payload

        with monkeypatch.context() as scoped:
            scoped.setattr(EffectExecutor, "capture_before", before_observer)
            scoped.setattr(RequestPayloadResolver, "resolve", resolver_observer)
            scoped.setattr(EffectExecutor, "capture_after", after_observer)
            LifecycleRunner.run_lifecycle(root, case_map, api_client_factory, credential_provider, request)

        effect = Context.get_effect(root["id"]) or {}
        if (effect.get("state") != Context.EFFECT_RESTORED
                or effect.get("restore_payload") != observations.get("restore_payload")):
            raise C1SafetyError("C1 lifecycle did not preserve restored Effect/recovery material")
        restored = capture_before(case=root, api_client_factory=api_client_factory,
                                  credential_provider=credential_provider, header_builder=CaseRunner.build_header)
        cur_lan = root["request_builder"]["arguments"]["cur_lan"]
        before_state = state.project(_read_model(observations["before"], username), cur_lan)
        mutation_state = state.project(_read_model(observations["mutation"], username), cur_lan)
        restored_state = state.project(_read_model(restored, username), cur_lan)
        mutation_diff, restored_diff = state.diff(before_state, mutation_state), state.diff(before_state, restored_state)
        _attach("C1 mutation semantic diff", {"changed_fields": sorted(mutation_diff),
                "before_sha256": sha256(before_state), "after_sha256": sha256(mutation_state)})
        _attach("C1 restored semantic diff", {"changed_fields": sorted(restored_diff),
                "before_sha256": sha256(before_state), "after_sha256": sha256(restored_state)})
        state.assert_mutation_only(before_state, mutation_state)
        state.assert_restored(before_state, restored_state)
        summary.update({"READY": "YES", "effect_state": effect["state"],
                        "rollback_executed": bool(effect.get("rollback_case_id")),
                        "mutation_changed_fields": sorted(mutation_diff), "restored_changed_fields": sorted(restored_diff),
                        "recovery_journal": journal.summary()})
        _attach("C1 lifecycle summary", summary)
        # Deletion is last: an evidence-writing exception must retain recovery material.
        journal.finish(success=True)
        summary["recovery_journal"] = journal.summary()
        return summary
    except Exception as exc:
        journal.finish(success=False)
        if "before" in observations and "mutation" in observations:
            try:
                cur_lan = root["request_builder"]["arguments"]["cur_lan"]
                before_state = state.project(_read_model(observations["before"], username), cur_lan)
                mutation_state = state.project(_read_model(observations["mutation"], username), cur_lan)
                summary["mutation_changed_fields"] = sorted(state.diff(before_state, mutation_state))
                _attach("C1 mutation semantic diff", {"changed_fields": summary["mutation_changed_fields"],
                        "before_sha256": sha256(before_state), "after_sha256": sha256(mutation_state)})
            except Exception:
                # Preserve the primary failure and sensitive recovery file even if observation fails.
                pass
        summary.update({"READY": "NO", "error_type": type(exc).__name__,
                        "effect_state": Context.get_effect_state(root["id"]), "recovery_journal": journal.summary()})
        if isinstance(exc, PendingRecoveryError):
            summary["pending_recovery"] = {"count": exc.count, "directory": str(exc.directory)}
        _attach("C1 lifecycle summary", summary)
        # Runtime assertion messages can contain PII; report only the recovery location.
        if isinstance(exc, PendingRecoveryError):
            raise C1AcceptanceError(str(exc)) from None
        raise C1AcceptanceError(f"C1 acceptance failed; recovery journal: {journal.summary()['path']}") from None
