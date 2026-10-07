"""
Effect Snapshot 收集器

v2.4.4B

职责：
1. 保存修改前数据。
2. 保存修改后数据。
3. 按逻辑字段 + JSONPath selector 提取真正的业务状态。
4. 生成字段变化 diff。

Capture Contract 示例：

    effect:
      target:
        fields:
          - usr_display_bil
      capture:
        fields:
          usr_display_bil: $.data.usr_display_bil
"""
import  copy

from jsonpath_ng import parse
class EffectCollector:
    @classmethod
    def build_snapshot(
            cls,
            before,
            after,
            fields=None,
            selectors=None,
    ):
        fields = fields or []
        selectors = selectors or {}

        if fields:
            before = cls.pick_fields(
                before,
                fields,
                selectors,
            )
            after = cls.pick_fields(
                after,
                fields,
                selectors,
            )
        return {
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
            "diff": cls.diff(before, after),
        }

    @classmethod
    def pick_fields(
            cls,
            data,
            fields,
            selectors=None,
    ):
        """
        从 Capture payload 中提取目标字段。

        selectors 存在时使用 JSONPath，例如：
            usr_display_bil -> $.data.usr_display_bil

        为兼容旧配置，如果字段没有 selector，则回退到顶层 dict.get(field)。
        但字段在顶层也不存在时会抛错，避免静默生成 None。
        """
        selectors = selectors or {}
        result = {}

        if not isinstance(data, dict):
            raise TypeError(
                "Effect Snapshot 仅支持 dict payload，"
                f"actual_type={type(data).__name__}"
            )

        for field in fields:
            expression = selectors.get(field)

            if expression:
                matches = parse(expression).find(data)
                if not matches:
                    raise ValueError(
                        f"Effect 字段提取失败: field={field}, "
                        f"JSONPath={expression}"
                    )

                if len(matches) == 1:
                    result[field] = matches[0].value
                else:
                    result[field] = [
                        match.value
                        for match in matches
                    ]
                continue

            if field not in data:
                raise ValueError(
                    f"Effect 字段提取失败: field={field} 不在响应顶层，"
                    "请在 effect.capture.fields 中配置 JSONPath"
                )

            result[field] = data[field]

        return result

    @staticmethod
    def diff(before, after):
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise TypeError("Effect diff 的 before / after 必须为 dict")

        changes = {}
        keys = set(before.keys()) | set(after.keys())

        for key in keys:
            old = before.get(key)
            new = after.get(key)

            if old != new:
                changes[key] = {
                    "from": old,
                    "to": new,
                }
        return changes