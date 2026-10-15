
"""
使用 classmethod 是为了正确操作类变量，并保持面向对象的灵活性。

staticmethod 适合实现与类相关但不依赖类状态的工具函数，而 classmethod 适合操作类级别的数据。
"""

"""
运行时 Context。

v2.4.4C 增加 Effect 状态机：
    unknown        请求已发生，但状态无法确认
    not_applied    before/after 已确认无变化
    applied        已确认存在副作用
    restored       已确认/声明恢复流程成功
    restore_failed 恢复流程失败，仍需要人工关注
"""
"""运行时 Context。

v2.4.5A 完成 Context V2：
1. variables 只存放运行时变量。
2. cases 只存放 Case 执行状态。
3. effects 只存放副作用生命周期状态。
4. 旧 Context.set/get 暂时保留为兼容 API，但内部统一转发到 variables，
   不再允许业务变量平铺到 Context 顶层。
"""

import  copy

from wizbank_api_test.core.runtime_context import get_runtime_context

_UNSET = object()

class Context:
    """
    pm.collectionVariables.set()
    pm.collectionVariables.get()
    """
    """当前执行作用域的运行时变量、Case 状态与 Effect 状态兼容接口。"""

    EFFECT_UNKNOWN = "unknown"
    EFFECT_NOT_APPLIED = "not_applied"
    EFFECT_APPLIED = "applied"
    EFFECT_RESTORED = "restored"
    EFFECT_RESTORE_FAILED = "restore_failed"

    _NAMESPACES = ("variables", "cases", "effects")
    @classmethod
    def _ensure_structure(cls):
        """解析当前 store，并保留标准命名空间的原有修复行为。"""
        runtime_context = get_runtime_context()
        if not isinstance(runtime_context.data, dict):
            runtime_context.data = {}

        for namespace in cls._NAMESPACES:
            value = runtime_context.data.get(namespace)
            if not isinstance(value, dict):
                runtime_context.data[namespace] = {}
        return runtime_context.data

    # （用于变量存取）
    @classmethod
    def set_variable(cls,key:str,value):
        data = cls._ensure_structure()
        data["variables"][key] = value

    # （用于变量存取）
    @classmethod
    def get_variable(cls,key:str,default=None):
        data = cls._ensure_structure()
        return data["variables"].get(key,default)
    @classmethod
    def has_variable(cls, key: str) -> bool:
        data = cls._ensure_structure()
        return key in data["variables"]

    @classmethod
    def clear_variables(cls):
        """只清理运行时变量。完整测试生命周期请使用 clear()。"""
        data = cls._ensure_structure()
        data["variables"] = {}

    # ------------------------------
    # cases
    # -----------------------------
    # （用于依赖检查）
    @classmethod
    def mark_case_passed(cls, case_id):
        data = cls._ensure_structure()
        case_state = data["cases"].setdefault(case_id, {})
        case_state["passed"] = True

    @classmethod
    def mark_cleanup_targets(cls, case_id, effect_case_ids):
        data = cls._ensure_structure()
        case_state = data["cases"].setdefault(case_id, {})
        case_state["cleanup_targets"] = sorted(set(effect_case_ids))

    @classmethod
    def get_cleanup_targets(cls, case_id):
        data = cls._ensure_structure()
        return list(
            data["cases"]
            .get(case_id, {})
            .get("cleanup_targets", [])
        )

    # （用于依赖检查）
    @classmethod
    def is_case_passed(cls, case_id):
        data = cls._ensure_structure()
        return (
            data["cases"]
            .get(case_id, {})
            .get("passed", False)
        )
    # ------------------------------
    # effects
    # ------------------------------
    # （用于标记和检查副作用）
    @classmethod
    def mark_effect(
            cls,
            case_id,
            metadata=None,
            snapshot=None,
            state=EFFECT_APPLIED,
            error=None,
            restore_payload=_UNSET,
    ):
        data = cls._ensure_structure()
        existing = data["effects"].get(case_id, {})
        if restore_payload is _UNSET and "restore_payload" in existing:
            restore_payload = existing["restore_payload"]
        data["effects"][case_id] = {
            "state": state,
            # 保留 applied 布尔值，兼容现有查看方式。
            "applied": state == cls.EFFECT_APPLIED,
            "metadata": metadata or {},
            "snapshot": snapshot or {},
        }
        if restore_payload is not _UNSET:
            data["effects"][case_id]["restore_payload"] = copy.deepcopy(restore_payload)
        if error:
            data["effects"][case_id]["error"] = str(error)

    @classmethod
    def promote_not_applied_to_unknown(cls, case_id, error):
        """保留完整恢复证据，仅提升带显式恢复材料的 NOT_APPLIED。"""
        effect = cls.get_effect(case_id)
        if (
            not effect
            or effect.get("state") != cls.EFFECT_NOT_APPLIED
            or not effect.get("restore_payload")
        ):
            return False

        effect["state"] = cls.EFFECT_UNKNOWN
        effect["applied"] = False
        effect["post_send_failure"] = {
            "reason": "post_send_processing_failed",
            "error_type": type(error).__name__,
        }
        return True

    @classmethod
    def mark_effect_restored(cls, case_id, rollback_case_id=None):
        data = cls._ensure_structure()
        effect = data["effects"].get(case_id)
        if not effect:
            return

        effect["state"] = cls.EFFECT_RESTORED
        effect["applied"] = False

        if rollback_case_id:
            effect["rollback_case_id"] = rollback_case_id

    @classmethod
    def mark_effect_restore_failed(
        cls,
        case_id,
        rollback_case_id=None,
        error=None,
    ):
        data = cls._ensure_structure()
        effect = data["effects"].get(case_id)
        if not effect:
            return

        effect["state"] = cls.EFFECT_RESTORE_FAILED
        effect["applied"] = True

        if rollback_case_id:
            effect["rollback_case_id"] = rollback_case_id

        if error:
            effect["restore_error"] = str(error)

    # （用于标记和检查副作用）
    @classmethod
    def has_effect(cls, case_id: str):
        """兼容旧语义：存在 effect 记录即返回 True。"""
        data = cls._ensure_structure()
        return case_id in data["effects"]

    @classmethod
    def get_effect(cls, case_id):
        data = cls._ensure_structure()
        return data["effects"].get(case_id)

    @classmethod
    def get_effect_state(cls, case_id):
        effect = cls.get_effect(case_id) or {}
        return effect.get("state")

    @classmethod
    def needs_cleanup(cls, case_id: str) -> bool:
        """Effect 处于这些状态时，rollback 必须获得执行机会。"""
        return cls.get_effect_state(case_id) in {
            cls.EFFECT_UNKNOWN,
            cls.EFFECT_APPLIED,
            cls.EFFECT_RESTORE_FAILED,
        }

    # ------------------------------
    # lifecycle / diagnostics
    # ------------------------------
    @classmethod
    def clear(cls):
        get_runtime_context().data={
            "variables": {},# 存放 {{xxx}} 变量
            "cases": {},# 存放用例执行状态（case_passed）
            "effects": {} # 存放“副作用”标记（destructive 是否已生效）
        }

    @classmethod
    def all(cls):
        data = cls._ensure_structure()
        return copy.deepcopy(data)

    @classmethod
    def snapshot(cls):
        data = cls._ensure_structure()
        return {
            "size": sum(
                len(data[namespace])
                for namespace in cls._NAMESPACES
            ),
            "data": copy.deepcopy(data),
        }

    # 兼容当前 Extractor / VariableResolver 的旧接口。
    # Context V2 的 variables 真正迁移会单独做版本，避免本次 Effect 改造范围过大。
    @classmethod
    def set(cls, key: str, value):
        # data[key] = value
        cls.set_variable(key, value)
    @classmethod
    def get(cls, key: str, default=None):
        # return data.get(key, default)
        return cls.get_variable(key, default)
