"""
用例规范化器（Case Normalizer）

职责：
    1. 将 case 中的 endpoint 引用合并为完整字段（method、path、headers 等）
    2. 合并 tags（endpoint 的 default_tags + case 的 tags，去重）
    3. 保留 case 中显式定义的字段覆盖 endpoint 默认值
    4. 为兼容旧用例，若 case 无 endpoint 且已有 method/path，则原样返回

合并规则：
    - endpoint 提供默认值：method, path, host, auth, headers, default_tags
    - case 可以覆盖任何字段（优先级高于 endpoint）
    - tags 合并：endpoint.default_tags + case.tags 去重，顺序为 endpoint 在前，case 在后
    - 若 case 中已定义 method/path，即使有 endpoint，也以 case 为准（供特殊覆盖场景使用）
"""
from typing import  Dict,Optional
from wizbank_api_test.core.endpoint_registry import EndpointRegistry

class CaseNormalizer:
    """用例规范化器（纯静态方法）"""

    @staticmethod
    def merge_case_with_endpoint(case: dict, endpoint: dict) -> dict:
        """
        将 endpoint 定义合并到 case 中

        合并策略：
            1. endpoint 中的所有字段作为基础值
            2. case 中的字段覆盖 endpoint 的对应字段
            3. tags 特殊处理：endpoint.default_tags + case.tags 合并去重

        参数：
            case: 原始用例字典（可能包含 endpoint 字段）
            endpoint: 接口定义字典（从 Registry 获取）

        返回：
            dict: 合并后的完整用例字典
        """
        # 复制 endpoint
        endpoint_id = case.get('endpoint')
        # 复制 endpoint 作为基础（避免修改原数据）
        merged = endpoint.copy()
        # 移除内部字段(如 _file)
        merged.pop("_file",None)

        # 移除 default_tags（已合并到 tags，不再需要）
        merged.pop("default_tags", None)

        # 用 case 的字段覆盖 endpoint 的字段
        for key, value in case.items():
            if key == "endpoint":
                continue  # endpoint 字段本身不保留到合并结果中
            merged[key] = value

        # ---------- tags 特殊合并 ----------
        endpoint_tags = list(endpoint.get("default_tags", []))
        case_tags = list(case.get("tags", []))

        # ----endpoint 是否存在 destructive True 存在合并---
        if endpoint.get("destructive") is True:
            endpoint_tags.append("destructive")

        # 合并去重（保留顺序：endpoint tags 在前，case tags 在后）
        combined_tags = []
        seen = set()
        for tag in endpoint_tags + case_tags:
            if tag not in seen:
                combined_tags.append(tag)
                seen.add(tag)

        merged["tags"] = combined_tags

        # ---------- headers 特殊合并 ----------
        # endpoint 和 case 都可能定义 headers，需要深度合并
        endpoint_headers = endpoint.get("headers", {}) or {}
        case_headers = case.get("headers", {})
        if endpoint_headers or case_headers:
            merged_headers = {}
            # 先放 endpoint 的，再被 case 覆盖
            merged_headers.update(endpoint_headers)
            merged_headers.update(case_headers)
            merged["headers"] = merged_headers

        # ---------- 确保 name 存在 ----------
        # 若 case 未提供 name，则使用 endpoint 的 name
        if "name" not in case and "name" in endpoint:
            merged["name"] = endpoint["name"]

        # ---------- 确保 module 存在 ----------
        if "module" not in case and "module" in endpoint:
            merged["module"] = endpoint["module"]

        # 保存case 对应的endpoint 来源
        if endpoint_id:
            merged["_endpoint_id"] = endpoint_id
        return merged

    @classmethod
    def normalize_case(cls, case: dict, registry: EndpointRegistry) -> dict:
        """
        规范化单条用例

        若 case 包含 endpoint 字段，则从 registry 获取 endpoint 定义并合并
        若 case 无 endpoint，则原样返回（兼容旧用例）

        参数：
            case: 原始用例字典
            registry: EndpointRegistry 实例

        返回：
            dict: 规范化后的完整用例字典
        """
        endpoint_id = case.get("endpoint")

        if endpoint_id:
            endpoint = registry.get(endpoint_id)
            return cls.merge_case_with_endpoint(case, endpoint)

        # 无 endpoint，直接返回原用例（但需确保有 method/path，后续 validator 会检查）
        return case.copy()