"""
新增
 - json/data/files 互斥检测（一个请求只能有一种 body 类型）
- GET 方法不允许配置请求体（json/data/files）
- auth 字段必须为合法值（admin/learner/none）

新增
- Content-Type 与请求体类型匹配检查
    * application/json      → 必须配置 json
    * x-www-form-urlencoded → 必须配置 data
    * multipart/form-data   → 必须配置 files

"""
from difflib import  get_close_matches
import re

from jsonpath_ng import  parse
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.utils.test_data_factory import TestDataFactory
class CaseValidator:
    """
    YAML 用例静态校验器

    目标：
        1. 在真正发请求前发现 YAML 配置错误（如缺少必填字段、ID 重复、依赖不存在等）。
        2. 避免执行到一半才发现依赖不存在、ID 重复、字段缺失。
        3. 为后续大规模接入接口做保护，提升测试稳定性。

    校验项：
        - 必填字段（id, name, method, path）
        - 重复用例 ID
        - HTTP 方法是否合法（GET/POST/PUT/PATCH/DELETE）
        - 优先级是否合法（P0-P4）
        - tags 是否为列表
        - order 是否为整数
        - depends_on 引用的用例是否存在
        - 是否自依赖（依赖自己）
        - 循环依赖检测
    """

    """用例校验器（纯静态方法）"""
    # 允许的HTTP方法
    ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
    # 允许的优先级
    ALLOWED_PRIORITIES = {"P0", "P1", "P2", "P3", "P4"}
    # 每个用例必须存在的字段
    REQUIRED_FIELDS = ["id", "name", "method", "path"]
    # 限制合法值
    ALLOWED_AUTHS = {"admin", "learner", "none", None}
    #
    ALLOWED_RISK_LEVELS = {"update", "delete", "send", "reset", "import", "upload", "batch"}

    # 新增 YAML 字段类型约束
    VALIDATION_RULE_ALLOWED_KEYS = {
        "status_code": {
            "type",
            "expected",
        },

        "contains": {
            "type",
            "expected",
        },

        "not_contains": {
            "type",
            "expected",
        },

        "json_path_exists": {
            "type",
            "expression",
        },

        "json_path_equals": {
            "type",
            "expression",
            "expected",
        },

        "json_path_type": {
            "type",
            "expression",
            "expected",
            "expected_type",
        },

        "json_path_not_empty": {
            "type",
            "expression",
        },

        "pagination": {
            "type",
            "total",
            "total_page",
            "page",
            "page_size",
            "rows",
            "expected_page",
            "expected_page_size",
            "check_total_page",
            "strict_rows",
        },

        "list_field_contains": {
            "type",
            "source",
            "field",
            "expected",
            "match",
            "case_sensitive",
        },
    }

    VALIDATION_RULE_REQUIRED_KEYS = {
        "status_code": {
            "expected",
        },

        "contains": {
            "expected",
        },

        "not_contains": {
            "expected",
        },

        "json_path_exists": {
            "expression",
        },

        "json_path_equals": {
            "expression",
            "expected",
        },

        "json_path_type": {
            "expression",
        },

        "json_path_not_empty": {
            "expression",
        },

        "pagination": set(),

        "list_field_contains": {
            "source",
            "field",
            "expected",
        },
    }
    @classmethod
    def validate_all(cls, cases: list):
        """
        执行所有校验，若存在错误则抛出 ValueError

        参数：
            cases: 用例字典列表（已包含 _case_file 字段）

        异常：
            ValueError: 汇总所有错误信息，按行列出
        """
        errors = []

        errors.extend(cls._validate_required_fields(cases))
        errors.extend(cls._validate_duplicate_ids(cases))
        errors.extend(cls._validate_method(cases))
        errors.extend(cls._validate_priority(cases))
        errors.extend(cls._validate_tags(cases))
        errors.extend(cls._validate_order(cases))
        errors.extend(cls._validate_depends_on_exists(cases))
        errors.extend(cls._validate_self_dependency(cases))
        errors.extend(cls._validate_cycle_dependency(cases))
        errors.extend(cls._validate_body_conflict(cases))
        errors.extend(cls._validate_get_body(cases))
        errors.extend(cls._validate_auth(cases))
        errors.extend(cls._validate_content_type_body_match(cases))
        errors.extend(cls._validate_destructive_contract(cases))
        errors.extend(cls._validate_effect_capture_contract(cases))
        errors.extend(cls._validate_effect_diff_contract(cases))
        errors.extend(cls._validate_update_contract_experiment(cases))
        errors.extend(cls._validate_data_factory_references(cases))
        errors.extend(cls._validate_restore_mapping_contract(cases))
        errors.extend(cls._validate_lifecycle_contract(cases))
        errors.extend(
            cls._validate_validation_rules(cases)
        )
        # 如果有错误，统一抛出
        if errors:
            message = "\n".join(f"- {err}" for err in errors)
            raise ValueError(f"YAML 用例静态校验失败：\n{message}")

    @classmethod
    def _validate_required_fields(cls, cases: list):
        """校验必填字段是否存在"""
        errors = []

        for index, case in enumerate(cases):
            case_id = case.get("id", f"UNKNOWN_INDEX_{index}")

            for field in cls.REQUIRED_FIELDS:
                if not case.get(field):
                    errors.append(f"{case_id}: 缺少必填字段 {field}")

        return errors

    @staticmethod
    def _validate_duplicate_ids(cases: list):
        """校验是否存在重复的用例 ID，并记录各自来源文件"""
        errors = []
        seen = {}

        for case in cases:
            case_id = case.get("id")
            if not case_id:
                continue

            if case_id in seen:
                errors.append(
                    f"重复用例 ID: {case_id}，文件1={seen[case_id]}, 文件2={case.get('_case_file')}"
                )
            else:
                seen[case_id] = case.get("_case_file")

        return errors

    @classmethod
    def _validate_method(cls, cases: list):
        """校验 HTTP 方法是否在允许列表中"""
        errors = []

        for case in cases:
            method = case.get("method")
            case_id = case.get("id", "UNKNOWN")

            if not method:
                continue

            if method.upper() not in cls.ALLOWED_METHODS:
                errors.append(f"{case_id}: method 不支持: {method}")

        return errors

    @classmethod
    def _validate_priority(cls, cases: list):
        """校验优先级是否合法（P0-P4），未设置则默认 P2 算通过"""
        errors = []

        for case in cases:
            priority = case.get("priority", "P2")
            case_id = case.get("id", "UNKNOWN")

            if priority not in cls.ALLOWED_PRIORITIES:
                errors.append(f"{case_id}: priority 不支持: {priority}")

        return errors

    @staticmethod
    def _validate_tags(cases: list):
        """校验 tags 字段必须为列表（若存在）"""
        errors = []

        for case in cases:
            tags = case.get("tags", [])
            case_id = case.get("id", "UNKNOWN")

            if tags is not None and not isinstance(tags, list):
                errors.append(f"{case_id}: tags 必须是 list")

        return errors

    @staticmethod
    def _validate_order(cases: list):
        """校验 order 字段必须为整数（若存在）"""
        errors = []

        for case in cases:
            order = case.get("order", 9999)
            case_id = case.get("id", "UNKNOWN")

            if not isinstance(order, int):
                errors.append(f"{case_id}: order 必须是 int，当前={order}")

        return errors

    @staticmethod
    def _normalize_depends(depends_on):
        """
        将 depends_on 标准化为列表
        支持：None、字符串、列表，其他类型返回一个特殊标记
        """
        if not depends_on:
            return []

        if isinstance(depends_on, str):
            return [depends_on]

        if isinstance(depends_on, list):
            return depends_on

        return ["__INVALID_DEPENDS_TYPE__"]

    @classmethod
    def _validate_depends_on_exists(cls, cases: list):
        """
        校验 depends_on 引用的用例 ID 是否真实存在
        同时检查 depends_on 类型是否为 str 或 list
        """
        errors = []
        case_ids = {case.get("id") for case in cases if case.get("id")}

        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            depends_on = cls._normalize_depends(case.get("depends_on", []))

            if "__INVALID_DEPENDS_TYPE__" in depends_on:
                errors.append(f"{case_id}: depends_on 类型必须是 str 或 list")
                continue

            for dep_id in depends_on:
                if dep_id not in case_ids:
                    errors.append(f"{case_id}: depends_on 引用了不存在的用例: {dep_id}")

        return errors

    @classmethod
    def _validate_self_dependency(cls, cases: list):
        """校验用例是否依赖自己（不允许）"""
        errors = []

        for case in cases:
            case_id = case.get("id")
            depends_on = cls._normalize_depends(case.get("depends_on", []))

            if case_id and case_id in depends_on:
                errors.append(f"{case_id}: 不允许依赖自己")

        return errors

    @classmethod
    def _validate_cycle_dependency(cls, cases: list):
        """
        校验是否存在循环依赖（如 A→B→C→A）
        使用 DFS 检测有向图中是否存在环
        """
        errors = []

        case_map = {
            case.get("id"): case
            for case in cases
            if case.get("id")
        }

        def visit(case_id, visiting, visited):
            if case_id in visiting:
                return [f"检测到循环依赖: {' -> '.join(list(visiting) + [case_id])}"]

            if case_id in visited:
                return []

            visiting.add(case_id)

            case = case_map.get(case_id, {})
            depends_on = cls._normalize_depends(case.get("depends_on", []))

            found_errors = []

            for dep_id in depends_on:
                if dep_id in case_map:
                    found_errors.extend(visit(dep_id, visiting, visited))

            visiting.remove(case_id)
            visited.add(case_id)

            return found_errors

        visited = set()

        for case_id in case_map:
            errors.extend(visit(case_id, set(), visited))

        return errors

    @staticmethod
    def _validate_body_conflict(cases:list):
        """
        校验 json/data/files 是否同时配置

        原因：在 requests 库中，json、data、files 是互斥的。
        若同时配置，requests 会忽略 data 只发送 json，容易造成混淆。
        因此应在 YAML 层面禁止同时配置。

        注意：此校验为阻断级错误。若特殊场景确实需要 multipart 混合字段，应使用 files + data 的专门结构，后续单独扩展。
        """
        errors = []

        for case in cases:
            case_id = case.get("id","UNKNOWN")

            has_json = "json" in case and case.get("json") is not None
            has_data = "data" in case and case.get("data") not in (None,{})
            has_files = "files" in case and case.get("files")  not in (None,{})

            body_count = sum([has_json,has_data,has_files])

            if body_count > 1:
                errors.append(
                    f"{case_id}:json/data/files 不建议同时配置，请确认清楚再配置"
                )
        return errors

    @staticmethod
    def _validate_get_body(cases:list):
        """
        校验 GET 方法是否配置了请求体

        RESTful 规范中，GET 方法不应携带请求体（Body）。
        虽然 HTTP 规范不禁止，但实践中大多数服务端会忽略或报错。

        应使用 params（URL 参数）代替。
        """
        errors = []

        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            method = str(case.get("method", "")).upper()

            if method == "GET":
                if  case.get("json") not in (None,{}):
                       errors.append(f"{case_id}:GET用例 不应配置json")
                if  case.get("data")  not in (None,{}):
                       errors.append(f"{case_id}:GET用例 不应配置data")
                if  case.get("files")  not in (None,{}):
                       errors.append(f"{case_id}:GET用例 不应配置files")

        return errors
    @classmethod
    def _validate_auth(cls,cases:list):
        """
        校验 auth 字段是否合法

        支持的认证方式：
            - admin   : 使用管理员 Cookie（由 admin_cookie 夹具提供）
            - learner : 使用学员 Cookie（可扩展）
            - none    : 不需要认证（或未设置）
            - None    : 同 none

        若需要扩展（如 teacher、guest），只需在 ALLOWED_AUTHS 中添加即可。
        """
        errors = []
        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            auth = case.get("auth")

            if auth not in cls.ALLOWED_AUTHS:
                errors.append(f"{case_id}:auth 不支持{auth}")
        return errors

    @staticmethod
    def _get_header_value(headers:dict,target_name:str):
        if not isinstance(headers,dict):
            return None
        for key,value in headers.items():
            if str(key).lower() == target_name.lower():
                if value is None:
                    return None
                return str(value).lower()
        return None

    @classmethod
    def _validate_content_type_body_match(cls,cases:list):
        errors = []
        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            headers = case.get("headers",{}) or {}
            content_type = cls._get_header_value(headers,"Content-Type")

            has_json = "json" in case and case.get("json") is not None
            has_data = "data" in case and case.get("data") not in (None, {})
            has_files = "files" in case and case.get("files") not in (None, {})

            if not content_type:
                continue
            # 正向检查：有 Content-Type 必须有对应 body
            if "application/json" in content_type and not has_json:
                    errors.append(f"{case_id}: Content-Type 为 application/json，但未配置 json")

            if "application/x-www-form-urlencoded" in content_type and not has_data:
                    errors.append(f"{case_id}: Content-Type 为 x-www-form-urlencoded，但未配置 data")

            if "multipart/form-data" in content_type and not has_files:
                    errors.append(f"{case_id}: Content-Type 为 multipart/form-data，但未配置 files")
            # 反向检查：有 body 但 Content-Type 不匹配（可选）
            if has_json and "application/json" not in content_type:
                errors.append(
                    f"{case_id}: 配置了 json，但 Content-Type 为 {content_type}，建议改为 application/json"
                )
        return errors

    @classmethod
    def _validate_destructive_contract(cls, cases: list):
        errors = []

        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            tags = case.get("tags", [])

            if "destructive" not in tags:
                continue

            risk_level = case.get("risk_level")

            if risk_level not in cls.ALLOWED_RISK_LEVELS:
                errors.append(
                    f"{case_id}: destructive 用例必须配置合法 risk_level"
                )

            if "readonly" in tags:
                errors.append(
                    f"{case_id}: destructive 用例不允许同时标记 readonly"
                )

            if not case.get("depends_on"):
                errors.append(
                    f"{case_id}: destructive 用例必须配置 depends_on，确保目标数据来自前置查询"
                )

        return errors

    @classmethod
    def _validate_effect_capture_contract(cls, cases: list):
        """
        v2.4.4B: 静态校验 Effect Capture Contract。

        目标是在真正 destructive 请求发送前发现：
        - Capture 仍在使用裸 URL
        - Endpoint ID 不存在
        - before / after 缺少业务参数
        - target.fields 没有 JSONPath selector
        - selector 本身不是合法 JSONPath
        """
        errors = []
        registry = EndpointRegistry()

        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            effect = case.get("effect")

            if not effect:
                continue

            if not isinstance(effect, dict):
                errors.append(f"{case_id}: effect 必须为 dict")
                continue

            capture = effect.get("capture")
            if not capture:
                continue

            if not isinstance(capture, dict):
                errors.append(f"{case_id}: effect.capture 必须为 dict")
                continue

            if capture.get("enabled", True) is False:
                continue

            target = effect.get("target", {}) or {}
            fields = target.get("fields", []) or []

            if not isinstance(fields, list) or not fields:
                errors.append(
                    f"{case_id}: effect.target.fields 必须为非空 list"
                )
                fields = []

            selectors = capture.get("fields", {}) or {}
            if not isinstance(selectors, dict):
                errors.append(
                    f"{case_id}: effect.capture.fields 必须为 dict，"
                    "格式 field: JSONPath"
                )
                selectors = {}

            for field in fields:
                expression = selectors.get(field)
                if not expression:
                    errors.append(
                        f"{case_id}: effect.capture.fields 缺少目标字段 "
                        f"'{field}' 的 JSONPath"
                    )
                    continue

                if not isinstance(expression, str):
                    errors.append(
                        f"{case_id}: effect.capture.fields.{field} "
                        "必须为 JSONPath 字符串"
                    )
                    continue

                try:
                    parse(expression)
                except Exception as exc:
                    errors.append(
                        f"{case_id}: effect.capture.fields.{field} "
                        f"JSONPath 非法: {expression}, error={exc}"
                    )

            for phase in ("before", "after"):
                phase_config = capture.get(phase)
                location = f"{case_id}.effect.capture.{phase}"

                if not isinstance(phase_config, dict):
                    errors.append(f"{location}: 必须为 dict")
                    continue

                endpoint_id = phase_config.get("endpoint")
                if not endpoint_id:
                    errors.append(f"{location}: 缺少 endpoint")
                    continue

                if not isinstance(endpoint_id, str):
                    errors.append(f"{location}.endpoint: 必须为字符串 Endpoint ID")
                    continue

                if endpoint_id.startswith("/"):
                    errors.append(
                        f"{location}.endpoint: 不允许使用裸 URL/path，"
                        "必须引用 Endpoint Registry ID"
                    )
                    continue

                if not registry.exists(endpoint_id):
                    errors.append(
                        f"{location}.endpoint: Endpoint 不存在: {endpoint_id}"
                    )

                expected_status = phase_config.get("expected_status", 200)
                if not isinstance(expected_status, int):
                    errors.append(
                        f"{location}.expected_status: 必须为 int"
                    )

                body_fields = [
                    key
                    for key in ("json", "data", "files")
                    if phase_config.get(key) is not None
                ]
                if len(body_fields) > 1:
                    errors.append(
                        f"{location}: json/data/files 只能配置一种，"
                        f"当前={body_fields}"
                    )

        return errors

    @classmethod
    def _validate_effect_diff_contract(cls, cases: list):
        """v2.4.6A: 校验 Effect Diff 白名单契约。

        仅用于更新契约专项：required 必须变化，allowed 之外不得变化。
        """
        errors = []
        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            effect = case.get("effect") or {}
            contract = effect.get("diff_contract")
            if contract is None:
                continue
            if not isinstance(contract, dict):
                errors.append(f"{case_id}.effect.diff_contract: 必须为 dict")
                continue

            required = contract.get("required", []) or []
            allowed = contract.get("allowed", []) or []
            if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
                errors.append(
                    f"{case_id}.effect.diff_contract.required: 必须为字符串 list"
                )
                continue
            if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
                errors.append(
                    f"{case_id}.effect.diff_contract.allowed: 必须为字符串 list"
                )
                continue

            target_fields = set(
                ((effect.get("target") or {}).get("fields") or [])
            )
            missing_target = (set(required) | set(allowed)) - target_fields
            if missing_target:
                errors.append(
                    f"{case_id}.effect.diff_contract: 字段未声明在 effect.target.fields: "
                    f"{sorted(missing_target)}"
                )
            if not set(required).issubset(set(allowed)):
                errors.append(
                    f"{case_id}.effect.diff_contract: required 必须属于 allowed"
                )
        return errors

    @classmethod
    def _validate_update_contract_experiment(cls, cases: list):
        """v2.4.6A: Partial Update 实验静态护栏。"""
        errors = []
        for case in cases:
            config = case.get("update_contract")
            if config is None:
                continue
            case_id = case.get("id", "UNKNOWN")
            if not isinstance(config, dict):
                errors.append(f"{case_id}.update_contract: 必须为 dict")
                continue
            if config.get("kind") != "partial_update":
                errors.append(
                    f"{case_id}.update_contract.kind: 当前仅支持 partial_update"
                )
            if case.get("contract_experiment") is not True:
                errors.append(
                    f"{case_id}.update_contract: 必须 contract_experiment=true"
                )
            if not case.get("lifecycle"):
                errors.append(
                    f"{case_id}.update_contract: 必须配置 lifecycle 保障恢复"
                )

            required = config.get("required_request_fields", []) or []
            allowed = config.get("allowed_request_fields", []) or []
            if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
                errors.append(
                    f"{case_id}.update_contract.required_request_fields: 必须为字符串 list"
                )
                continue
            if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
                errors.append(
                    f"{case_id}.update_contract.allowed_request_fields: 必须为字符串 list"
                )
                continue
            body = case.get("json")
            if not isinstance(body, dict):
                errors.append(
                    f"{case_id}.update_contract: Partial Update 实验必须使用 json body"
                )
                continue
            body_fields = set(body)
            missing = set(required) - body_fields
            extra = body_fields - set(allowed)
            if missing:
                errors.append(
                    f"{case_id}.update_contract: 缺少最小请求字段 {sorted(missing)}"
                )
            if extra:
                errors.append(
                    f"{case_id}.update_contract: 实验请求包含未授权字段 {sorted(extra)}"
                )

            effect = case.get("effect") or {}
            if not effect.get("diff_contract"):
                errors.append(
                    f"{case_id}.update_contract: 必须配置 effect.diff_contract"
                )
        return errors

    @classmethod
    def _validate_data_factory_references(cls, cases: list):
        """v2.4.4D: 静态校验 {{$data.xxx}} 引用。"""
        errors = []
        supported = TestDataFactory.supported_keys()
        pattern = re.compile(r"\{\{\$data\.([A-Za-z_][A-Za-z0-9_]*)\}\}")

        def walk(value, location):
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(item, f"{location}.{key}")
                return

            if isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{location}[{index}]")
                return

            if not isinstance(value, str):
                return

            if "{{$data." in value and not pattern.search(value):
                errors.append(
                    f"{location}: $data 引用格式非法，应使用 "
                    "{{$data.key}}"
                )
                return

            for match in pattern.finditer(value):
                key = match.group(1)
                if key not in supported:
                    candidates = get_close_matches(
                        key,
                        supported,
                        n=1,
                        cutoff=0.6,
                    )
                    suffix = (
                        f"，是否想写 '{candidates[0]}'?"
                        if candidates
                        else ""
                    )
                    errors.append(
                        f"{location}: 不支持的 $data 键 '{key}'{suffix}"
                    )

        for case in cases:
            walk(case, case.get("id", "UNKNOWN"))

        return errors

    @classmethod
    def _validate_restore_mapping_contract(cls, cases: list):
        """v2.4.4D: 静态校验显式 restore_mapping。"""
        errors = []
        case_map = {
            case.get("id"): case
            for case in cases
            if case.get("id")
        }

        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            config = case.get("restore_mapping")
            if config is None:
                continue

            if not isinstance(config, dict):
                errors.append(f"{case_id}.restore_mapping: 必须为 dict")
                continue

            tags = case.get("tags", []) or []
            if "rollback" not in tags:
                errors.append(
                    f"{case_id}.restore_mapping: 仅允许用于 rollback Case"
                )

            if case.get("destructive") is not True:
                errors.append(
                    f"{case_id}.restore_mapping: rollback 写 Case 必须 destructive=true"
                )

            source_effect = config.get("source_effect")
            fields = config.get("fields")

            if not isinstance(source_effect, str) or not source_effect:
                errors.append(
                    f"{case_id}.restore_mapping.source_effect: 必须为非空 Case ID"
                )
                continue

            source_case = case_map.get(source_effect)
            if not source_case:
                errors.append(
                    f"{case_id}.restore_mapping.source_effect: "
                    f"Case 不存在: {source_effect}"
                )
                continue

            depends = cls._normalize_depends(case.get("depends_on", []))
            if source_effect not in depends:
                errors.append(
                    f"{case_id}.restore_mapping.source_effect: {source_effect} "
                    "必须同时出现在 depends_on 中"
                )

            source_effect_config = source_case.get("effect", {}) or {}
            source_target = source_effect_config.get("target", {}) or {}
            source_fields = set(source_target.get("fields", []) or [])
            source_capture = source_effect_config.get("capture", {}) or {}
            source_selectors = source_capture.get("fields", {}) or {}

            if not source_fields:
                errors.append(
                    f"{case_id}.restore_mapping.source_effect: {source_effect} "
                    "没有可恢复的 effect.target.fields"
                )

            if not isinstance(fields, dict) or not fields:
                errors.append(
                    f"{case_id}.restore_mapping.fields: 必须为非空 dict"
                )
                continue

            for target_path, source_path in fields.items():
                location = (
                    f"{case_id}.restore_mapping.fields[{target_path!r}]"
                )

                if not isinstance(target_path, str) or "." not in target_path:
                    errors.append(
                        f"{location}: target 必须类似 json.usr_display_bil"
                    )
                    continue

                target_root = target_path.split(".", 1)[0]
                if target_root not in {"json", "data", "params"}:
                    errors.append(
                        f"{location}: target 根节点仅支持 json/data/params"
                    )

                if not isinstance(source_path, str) or not source_path.startswith("before."):
                    errors.append(
                        f"{location}: source 必须显式引用 before.xxx"
                    )
                    continue

                source_field = source_path[len("before."):]
                if "." in source_field or not source_field:
                    errors.append(
                        f"{location}: 当前版本 source 仅支持 before.<逻辑字段>"
                    )
                    continue

                if source_field not in source_fields:
                    errors.append(
                        f"{location}: source 字段 '{source_field}' "
                        f"未声明在 {source_effect}.effect.target.fields"
                    )

                if source_field not in source_selectors:
                    errors.append(
                        f"{location}: source 字段 '{source_field}' "
                        f"缺少 {source_effect}.effect.capture.fields selector"
                    )

        return errors

    @classmethod
    def _validate_lifecycle_contract(cls, cases: list):
        """v2.4.5C: 静态校验显式四阶段 lifecycle。"""
        errors = []
        case_map = {
            case.get("id"): case
            for case in cases
            if case.get("id")
        }
        managed_children = {}
        required_keys = {"verify", "rollback", "verify_rollback"}

        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            lifecycle = case.get("lifecycle")
            if lifecycle is None:
                continue

            if not isinstance(lifecycle, dict):
                errors.append(f"{case_id}.lifecycle: 必须为 dict")
                continue

            unknown_keys = set(lifecycle) - required_keys
            for key in sorted(unknown_keys):
                errors.append(
                    f"{case_id}.lifecycle: 不支持字段 '{key}'"
                )

            missing_keys = [
                key for key in sorted(required_keys)
                if not lifecycle.get(key)
            ]
            if missing_keys:
                errors.append(
                    f"{case_id}.lifecycle: 缺少字段 {missing_keys}"
                )
                continue
            if not (
                    case.get("destructive") is True
                    or "destructive" in (case.get("tags", []) or [])
            ):
                errors.append(
                    f"{case_id}.lifecycle: root Case 必须带 destructive Endpoint/Case"
                )

            if not case.get("effect"):
                errors.append(
                    f"{case_id}.lifecycle: root Case 必须配置 effect"
                )

            verify_id = lifecycle["verify"]
            rollback_id = lifecycle["rollback"]
            verify_rollback_id = lifecycle["verify_rollback"]

            child_ids = {verify_id, rollback_id, verify_rollback_id}
            if case_id in child_ids:
                errors.append(
                    f"{case_id}.lifecycle: root 不允许引用自己"
                )

            for role, child_id in lifecycle.items():
                child = case_map.get(child_id)
                location = f"{case_id}.lifecycle.{role}"

                if not isinstance(child_id, str) or not child_id:
                    errors.append(f"{location}: 必须为非空 Case ID")
                    continue

                if child is None:
                    errors.append(f"{location}: Case 不存在: {child_id}")
                    continue

                previous_root = managed_children.get(child_id)
                if previous_root and previous_root != case_id:
                    errors.append(
                        f"{location}: {child_id} 已被 lifecycle root "
                        f"{previous_root} 管理"
                    )
                else:
                    managed_children[child_id] = case_id

                if child.get("lifecycle"):
                    errors.append(
                        f"{location}: managed child 不允许再声明 lifecycle"
                    )

            verify_case = case_map.get(verify_id)
            rollback_case = case_map.get(rollback_id)
            verify_rollback_case = case_map.get(verify_rollback_id)

            if verify_case:
                verify_depends = cls._normalize_depends(
                    verify_case.get("depends_on", [])
                )
                if case_id not in verify_depends:
                    errors.append(
                        f"{case_id}.lifecycle.verify: {verify_id} "
                        f"必须 depends_on {case_id}"
                    )
                tags = verify_case.get("tags", []) or []
                if "verify" not in tags:
                    errors.append(
                        f"{case_id}.lifecycle.verify: {verify_id} 必须带 verify tag"
                    )

            if rollback_case:
                rollback_depends = cls._normalize_depends(
                    rollback_case.get("depends_on", [])
                )
                if case_id not in rollback_depends:
                    errors.append(
                        f"{case_id}.lifecycle.rollback: {rollback_id} "
                        f"必须 depends_on {case_id}"
                    )

                tags = rollback_case.get("tags", []) or []
                if "rollback" not in tags:
                    errors.append(
                        f"{case_id}.lifecycle.rollback: {rollback_id} "
                        "必须带 rollback tag"
                    )
                if rollback_case.get("destructive") is not True:
                    errors.append(
                        f"{case_id}.lifecycle.rollback: {rollback_id} "
                        "必须 destructive=true"
                    )

                restore_mapping = rollback_case.get("restore_mapping") or {}
                if restore_mapping.get("source_effect") != case_id:
                    errors.append(
                        f"{case_id}.lifecycle.rollback: {rollback_id} "
                        "restore_mapping.source_effect 必须指向 lifecycle root"
                    )

            if verify_rollback_case:
                verify_rb_depends = cls._normalize_depends(
                    verify_rollback_case.get("depends_on", [])
                )
                if rollback_id not in verify_rb_depends:
                    errors.append(
                        f"{case_id}.lifecycle.verify_rollback: "
                        f"{verify_rollback_id} 必须 depends_on {rollback_id}"
                    )

                tags = verify_rollback_case.get("tags", []) or []
                if "rollback" not in tags or "verify" not in tags:
                    errors.append(
                        f"{case_id}.lifecycle.verify_rollback: "
                        f"{verify_rollback_id} 必须同时带 rollback/verify tag"
                    )

        return errors
    #
    @classmethod
    def _validate_validation_rules(cls, cases: list):
        errors = []

        for case in cases:
            case_id = case.get("id", "UNKNOWN")
            validations = case.get("validate", [])

            if validations is None:
                continue

            if not isinstance(validations, list):
                errors.append(
                    f"{case_id}: validate 必须为 list"
                )
                continue

            for index, rule in enumerate(validations):
                rule_location = (
                    f"{case_id}.validate[{index}]"
                )

                if not isinstance(rule, dict):
                    errors.append(
                        f"{rule_location}: "
                        f"校验规则必须为 dict"
                    )
                    continue

                rule_type = rule.get("type")

                if not rule_type:
                    errors.append(
                        f"{rule_location}: 缺少 type"
                    )
                    continue

                if rule_type not in cls.VALIDATION_RULE_ALLOWED_KEYS:
                    errors.append(
                        f"{rule_location}: "
                        f"不支持的校验类型 {rule_type}"
                    )
                    continue

                allowed_keys = (
                    cls.VALIDATION_RULE_ALLOWED_KEYS[
                        rule_type
                    ]
                )

                required_keys = (
                    cls.VALIDATION_RULE_REQUIRED_KEYS[
                        rule_type
                    ]
                )

                # --------------------------------
                # 未知字段
                # --------------------------------

                unknown_keys = (
                        set(rule.keys())
                        - allowed_keys
                )

                for key in sorted(unknown_keys):
                    candidates = get_close_matches(
                        key,
                        allowed_keys,
                        n=1,
                        cutoff=0.6,
                    )

                    if candidates:
                        errors.append(
                            f"{rule_location}: "
                            f"{rule_type} 存在未知字段 "
                            f"'{key}'，"
                            f"是否想写 '{candidates[0]}'?"
                        )
                    else:
                        errors.append(
                            f"{rule_location}: "
                            f"{rule_type} 存在未知字段 "
                            f"'{key}'"
                        )

                # --------------------------------
                # 必填字段
                # --------------------------------

                for key in required_keys:
                    if key not in rule:
                        errors.append(
                            f"{rule_location}: "
                            f"{rule_type} 缺少字段 "
                            f"'{key}'"
                        )

                # --------------------------------
                # json_path_type 特殊契约
                # --------------------------------

                if rule_type == "json_path_type":
                    if (
                            "expected_type" not in rule
                            and "expected" not in rule
                    ):
                        errors.append(
                            f"{rule_location}: "
                            f"json_path_type 必须配置 "
                            f"expected_type"
                        )

                # --------------------------------
                # pagination 特殊契约
                # --------------------------------

                if rule_type == "pagination":

                    for key in (
                            "check_total_page",
                            "strict_rows",
                    ):
                        if (
                                key in rule
                                and not isinstance(
                            rule[key],
                            bool
                        )
                        ):
                            errors.append(
                                f"{rule_location}: "
                                f"{key} 必须为 bool"
                            )

                    for key in (
                            "expected_page",
                            "expected_page_size",
                    ):
                        if key in rule:
                            value = rule[key]

                            if (
                                    not isinstance(value, int)
                                    or isinstance(value, bool)
                            ):
                                errors.append(
                                    f"{rule_location}: "
                                    f"{key} 必须为 int"
                                )

                # --------------------------------
                # list_field_contains 契约
                # --------------------------------

                if rule_type == "list_field_contains":

                    match = rule.get(
                        "match",
                        "any",
                    )

                    if match not in {
                        "any",
                        "all",
                    }:
                        errors.append(
                            f"{rule_location}: "
                            f"match 仅支持 any/all，"
                            f"actual={match}"
                        )

                    if (
                            "case_sensitive" in rule
                            and not isinstance(
                        rule["case_sensitive"],
                        bool
                    )
                    ):
                        errors.append(
                            f"{rule_location}: "
                            f"case_sensitive 必须为 bool"
                        )

        return errors