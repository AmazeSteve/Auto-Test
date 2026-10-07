"""跨 Case 生命周期编排器。

v2.4.5C：
- CaseRunner 只执行单条 Case。
- LifecycleRunner 负责依赖、destructive 策略、四阶段生命周期与 cleanup。

生命周期：
    modify -> verify -> rollback -> verify_rollback

rollback 放在 finally 中，因此 modify / verify 任意阶段失败，只要 Effect 表明
存在 applied / unknown / restore_failed 状态，恢复流程仍会获得执行机会。
"""
import json
import  os

import allure
import  pytest


from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.case_selector import CaseSelector
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_restore import EffectRestoreResolver
from wizbank_api_test.utils.redaction import SensitiveDataRedactor
from wizbank_api_test.core.effect_collector import EffectCollector
from wizbank_api_test.core.response_parser import ResponseParser

class LifecycleCleanupError(RuntimeError):
    """生命周期主流程与恢复流程至少有一个失败。"""

    def __init__(self, root_case_id, primary_error=None, cleanup_error=None):
        self.root_case_id = root_case_id
        self.primary_error = primary_error
        self.cleanup_error = cleanup_error

        parts = [f"生命周期 {root_case_id} 恢复失败"]
        if primary_error is not None:
            parts.append(
                f"primary={type(primary_error).__name__}: {primary_error}"
            )
        if cleanup_error is not None:
            parts.append(
                f"cleanup={type(cleanup_error).__name__}: {cleanup_error}"
            )

        super().__init__("; ".join(parts))


class LifecycleRunner:
    """处理普通依赖与 destructive 四阶段生命周期。"""

    REQUIRED_LIFECYCLE_KEYS = (
        "verify",
        "rollback",
        "verify_rollback",
    )
    # 生命周期标记
    PHASE_LABELS = {
        "modify": "修改",
        "verify": "验证修改",
        "rollback": "回滚",
        "verify_rollback": "验证回滚",
    }

    @staticmethod
    def _allow_destructive(request) ->bool:
        return bool(
            request.config.getoption("--allow-destructive")
        )

    @classmethod
    def _check_destructive_permission(cls,case:dict,request):
        tags  = case.get("tags", []) or []
        is_destructive = (
            case.get("destructive") is True
            or "destructive" in tags
        )
        if is_destructive and not cls._allow_destructive(request):
            pytest.skip("destructive 用例默认跳过，需要 --allow-destructive 才执行")

    @classmethod
    def _check_contract_experiment_permission(cls, case: dict, request):
        """v2.4.6A: 契约实验采用额外四重门禁。

        只有明确满足以下条件时实验 Case 才能执行写请求：
        1. Case 显式 contract_experiment=true。
        2. 命令行带 --allow-contract-experiment。
        3. --tag update_contract，避免常规全量运行误触实验。
        4. 环境变量 TEST_USERNAME 显式设置，禁止依赖默认测试账号。
        """
        if case.get("contract_experiment") is not True:
            return

        if not request.config.getoption("--allow-contract-experiment"):
            pytest.skip(
                "接口契约实验默认跳过，需要 --allow-contract-experiment 才执行"
            )

        target_tag = request.config.getoption("--tag")
        if target_tag != "update_contract":
            pytest.skip(
                "接口契约实验仅允许通过 --tag update_contract 显式选择"
            )

        if not os.getenv("TEST_USERNAME"):
            pytest.skip(
                "接口契约实验要求显式设置 TEST_USERNAME，禁止使用默认测试账号"
            )

    @classmethod
    def check_dependencies(cls,case:dict,bypass_case_passed=None):
        """检查普通 depends_on；指定 bypass 的依赖不要求 passed。"""
        depends_on = CaseSelector.normalize_depends(case.get("depends_on", []))
        bypass = set(bypass_case_passed or [])
        for dep_case_id in depends_on:
            # if Context.get(f"case_passed.{dep_case_id}")is not True:
            if dep_case_id in bypass:
                continue
            if not Context.is_case_passed(dep_case_id):
                pytest.skip(f"前置用例未通过或未执行:{dep_case_id}")

        return depends_on
    @classmethod
    def get_cleanup_dependencies(cls, rollback_case: dict):
        """找出 rollback 依赖中当前真正需要 cleanup 的 Effect。"""
        depends = CaseSelector.normalize_depends(
            rollback_case.get("depends_on", [])
        )
        return {
            dep_case_id
            for dep_case_id in depends
            if Context.needs_cleanup(dep_case_id)
        }

    @staticmethod
    def _attach_restore_evidence(restore_evidence):
        if not restore_evidence:
            return
        allure.attach(
            json.dumps(
                SensitiveDataRedactor.redact(restore_evidence),
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            name="restore mapping resolved",
            attachment_type=allure.attachment_type.JSON,
        )
    @classmethod
    def _prepare_runtime_case(cls, case: dict):
        runtime_case, restore_evidence = EffectRestoreResolver.apply(case)
        cls._attach_restore_evidence(restore_evidence)
        return runtime_case

    @classmethod
    def run_case(cls,case:dict,api_client_factory,admin_cookie: str,request,*,bypass_case_passed=None,apply_metadata:bool = True):
        """运行普通单 Case，生命周期策略仍由本层统一检查。"""
        cls._check_destructive_permission(case,request)
        cls._check_contract_experiment_permission(case, request)
        depends_on = cls.check_dependencies(case,bypass_case_passed=bypass_case_passed)
        runtime_case = cls._prepare_runtime_case(case)

        return CaseRunner.run(
            runtime_case,
            api_client_factory,
            admin_cookie,
            apply_metadata=apply_metadata,
            depends_on=depends_on,
        )
    @classmethod
    def lifecycle_child_map(cls, cases):
        """返回 child_case_id -> root_case_id 显式映射。"""
        result = {}

        for case in cases:
            lifecycle = case.get("lifecycle") or {}
            if not isinstance(lifecycle, dict):
                continue

            root_id = case.get("id")
            for key in cls.REQUIRED_LIFECYCLE_KEYS:
                child_id = lifecycle.get(key)
                if child_id:
                    result[child_id] = root_id

        return result

    @classmethod
    def get_managed_child_ids(cls, cases):
        return set(cls.lifecycle_child_map(cases))

    @classmethod
    def _resolve_lifecycle_cases(cls, root_case: dict, case_map: dict):
        lifecycle = root_case.get("lifecycle") or {}

        missing = [
            key
            for key in cls.REQUIRED_LIFECYCLE_KEYS
            if not lifecycle.get(key)
        ]
        if missing:
            raise ValueError(
                f"{root_case.get('id')} lifecycle 缺少字段: {missing}"
            )

        resolved = {}
        for key in cls.REQUIRED_LIFECYCLE_KEYS:
            case_id = lifecycle[key]
            if case_id not in case_map:
                raise ValueError(
                    f"{root_case.get('id')} lifecycle.{key} Case 不存在: {case_id}"
                )
            resolved[key] = case_map[case_id]

        return resolved

    @classmethod
    def _run_phase(
            cls,
            phase: str,
            case: dict,
            api_client_factory,
            admin_cookie: str,
            request,
            *,
            bypass_case_passed=None,
    ):
        label = cls.PHASE_LABELS[phase]
        with allure.step(
                f"Lifecycle {label}: {case.get('id')} - {case.get('name')}"
        ):
            return cls.run_case(
                case,
                api_client_factory,
                admin_cookie,
                request,
                bypass_case_passed=bypass_case_passed,
                apply_metadata=False,
            )

    @classmethod
    def _verify_contract_restore_snapshot(cls, root_case: dict, response):
        """v2.4.6A: 回滚后把完整受监控状态与 root.before 独立比较。

        只在 update_contract.verify_restore_snapshot=true 时启用。
        复用 Effect Capture 的 fields/selectors，避免再维护一份字段契约。
        """
        contract = root_case.get("update_contract") or {}
        if contract.get("verify_restore_snapshot") is not True:
            return

        effect = Context.get_effect(root_case["id"]) or {}
        snapshot = effect.get("snapshot") or {}
        expected_before = snapshot.get("before")
        if not isinstance(expected_before, dict):
            raise AssertionError(
                f"{root_case['id']} 缺少可用于恢复校验的 before snapshot"
            )

        effect_config = root_case.get("effect", {}) or {}
        target = effect_config.get("target", {}) or {}
        capture = effect_config.get("capture", {}) or {}
        fields = target.get("fields", []) or []
        selectors = capture.get("fields", {}) or {}
        payload = ResponseParser.parse_json(response)
        restored = EffectCollector.pick_fields(payload, fields, selectors)
        restore_diff = EffectCollector.diff(expected_before, restored)

        evidence = {
            "expected_before": expected_before,
            "restored": restored,
            "diff": restore_diff,
        }
        allure.attach(
            json.dumps(
                SensitiveDataRedactor.redact(evidence),
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            name="update contract restore snapshot comparison",
            attachment_type=allure.attachment_type.JSON,
        )
        if restore_diff:
            raise AssertionError(
                f"{root_case['id']} 回滚后状态未完整恢复: "
                f"fields={sorted(restore_diff)}"
            )

    @classmethod
    def _mark_restore_failed(
            cls,
            effect_case_id: str,
            rollback_case_id: str,
            error,
    ):
        Context.mark_effect_restore_failed(
            case_id=effect_case_id,
            rollback_case_id=rollback_case_id,
            error=error,
        )

    @classmethod
    def run_lifecycle(
            cls,
            root_case: dict,
            case_map: dict,
            api_client_factory,
            admin_cookie: str,
            request,
    ):
        """在一个 pytest 调用栈内执行完整 A -> B -> A 生命周期。"""
        cls._check_destructive_permission(root_case, request)
        root_depends = cls.check_dependencies(root_case)
        lifecycle_cases = cls._resolve_lifecycle_cases(
            root_case,
            case_map,
        )

        root_id = root_case["id"]
        rollback_case = lifecycle_cases["rollback"]

        host_key = root_case.get("host", "Host-be")
        CaseRunner.apply_allure_metadata(
            root_case,
            host_key,
            depends_on=root_depends,
        )
        allure.dynamic.parameter("lifecycle_root", root_id)
        allure.dynamic.parameter(
            "lifecycle_verify",
            lifecycle_cases["verify"]["id"],
        )
        allure.dynamic.parameter(
            "lifecycle_rollback",
            rollback_case["id"],
        )
        allure.dynamic.parameter(
            "lifecycle_verify_rollback",
            lifecycle_cases["verify_rollback"]["id"],
        )

        primary_error = None
        cleanup_error = None

        try:
            cls._run_phase(
                "modify",
                root_case,
                api_client_factory,
                admin_cookie,
                request,
            )
            cls._run_phase(
                "verify",
                lifecycle_cases["verify"],
                api_client_factory,
                admin_cookie,
                request,
            )
        except Exception as exc:
            primary_error = exc
        finally:
            if Context.needs_cleanup(root_id):
                try:
                    cleanup_depends = cls.get_cleanup_dependencies(
                        rollback_case
                    )
                    if root_id not in cleanup_depends:
                        cleanup_depends.add(root_id)

                    cls._run_phase(
                        "rollback",
                        rollback_case,
                        api_client_factory,
                        admin_cookie,
                        request,
                        bypass_case_passed=cleanup_depends,
                    )
                    Context.mark_cleanup_targets(
                        rollback_case["id"],
                        cleanup_depends,
                    )

                    verify_rollback_response = cls._run_phase(
                        "verify_rollback",
                        lifecycle_cases["verify_rollback"],
                        api_client_factory,
                        admin_cookie,
                        request,
                    )
                    cls._verify_contract_restore_snapshot(
                        root_case,
                        verify_rollback_response,
                    )
                    Context.mark_effect_restored(
                        root_id,
                        rollback_case_id=rollback_case["id"],
                    )
                except Exception as exc:
                    cleanup_error = exc
                    cls._mark_restore_failed(
                        effect_case_id=root_id,
                        rollback_case_id=rollback_case["id"],
                        error=exc,
                    )

        if cleanup_error is not None:
            raise LifecycleCleanupError(
                root_case_id=root_id,
                primary_error=primary_error,
                cleanup_error=cleanup_error,
            ) from cleanup_error

        if primary_error is not None:
            raise primary_error

        return Context.get_effect(root_id)
