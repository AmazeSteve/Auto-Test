"""显式 Effect 恢复映射解析器。

restore_mapping 示例：

restore_mapping:
  source_effect: WB_USER_DESTRUCTIVE_DEMO_001
  fields:
    json.usr_display_bil: before.usr_display_bil

设计原则：
1. 恢复值只能来自已记录的 Effect Snapshot。
2. 映射必须显式声明，禁止根据字段名自动猜测。
3. 仅负责把 Snapshot 值写入运行期 Case 副本，不发送请求。
"""
import copy
from wizbank_api_test.core.context import Context

class EffectRestoreResolver:
    """根据 restore_mapping 构造 rollback Case 的运行期请求数据。"""
    ALLOWED_TARGET_ROOTS={"json","data","params"}

    @classmethod
    def apply (cls, case:dict):
        config = case.get("restore_mapping")
        if not config:
            return copy.deepcopy(case), {}
        source_effect = config.get("source_effect")
        fields = config.get("fields",{}) or {}

        effect = Context.get_effect(source_effect)
        if not effect:
            raise ValueError(
                f"{case.get('id', 'UNKNOWN')} restore_mapping 找不到 Effect: "
                f"{source_effect}"
            )
        snapshot = effect.get("snapshot") or {}
        runtime_case = copy.deepcopy(case)
        evidence = {}
        for target_path, source_path in fields.items():
            value = cls._get_path(snapshot, source_path)
            cls._set_path(runtime_case, target_path, copy.deepcopy(value))
            evidence[target_path] = {
                "source": f"{source_effect}.snapshot.{source_path}",
                "value": copy.deepcopy(value),
            }

        return runtime_case, evidence

    @staticmethod
    def _get_path(data: dict, path: str):
        current = data
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise ValueError(
                    f"restore_mapping source 不存在: {path}"
                )
            current = current[part]
        return current

    @classmethod
    def _set_path(cls, data: dict, path: str, value):
        parts = path.split(".")
        if len(parts) < 2:
            raise ValueError(
                f"restore_mapping target 必须包含请求区域和字段路径: {path}"
            )

        root = parts[0]
        if root not in cls.ALLOWED_TARGET_ROOTS:
            raise ValueError(
                f"restore_mapping target 根节点不支持: {root}"
            )

        current = data
        for part in parts[:-1]:
            child = current.get(part)
            if child is None:
                child = {}
                current[part] = child
            elif not isinstance(child, dict):
                raise ValueError(
                    f"restore_mapping target 路径不可写入: {path}"
                )
            current = child

        current[parts[-1]] = value