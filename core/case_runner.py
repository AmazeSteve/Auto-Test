"""单条 YAML Case 执行器。

v2.4.5C 职责收口：

CaseRunner 只负责一条 Case 自己的执行：
1. Allure 元数据。
2. 请求变量解析与认证头构造。
3. 单 Case Effect before / after Capture 与 Snapshot。
4. 发送请求。
5. 断言。
6. 提取变量。
7. 标记当前 Case passed。

依赖判断、destructive 执行策略、rollback 触发、restore_failed / restored
等跨 Case 生命周期逻辑全部交给 LifecycleRunner。
"""

import  json

import  allure



from wizbank_api_test.core.assertions import Assertor
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_collector import EffectCollector
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.extractor import Extractor
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.utils.redaction import SensitiveDataRedactor
class CaseRunner:
    """
    单条YAML用例执行器
    """
    @staticmethod
    def map_priority_to_severity(priority: str) -> str:
        """
        将用例优先级映射为 Allure 严重级别

        映射关系：
            P0 -> blocker   (阻塞)
            P1 -> critical  (严重)
            P2 -> normal    (正常)  默认
            P3 -> minor     (次要)
            P4 -> trivial   (微小)
        """
        mapping = {
            "P0": "blocker",
            "P1": "critical",
            "P2": "normal",
            "P3": "minor",
            "P4": "trivial",
        }
        # 没找到默认normal
        return mapping.get(priority, "normal")



    @classmethod
    def apply_allure_metadata(cls,case:dict,host_key:str,depends_on=None):
        depends_on = list(depends_on or case.get("depends_on",[])or [])
        # 使用动态方式设置 allure 报告中的 feature、story 和 title
        allure.dynamic.feature(case.get("module", "未分类模块"))
        allure.dynamic.story(case.get("name", "未命名用例"))
        allure.dynamic.title(f"{case.get('id')} - {case.get('name')}")

        # 增加新标记 优先级 所属者 从用例中取出
        allure.dynamic.severity(cls.map_priority_to_severity(case.get("priority", "P2")))

        allure.dynamic.label("owner", case.get("owner", "unknown"))

        allure.dynamic.parameter("case", case.get('id'))
        # Endpoint 显示在ALLURE
        endpoint_id = case.get("_endpoint_id")
        if endpoint_id:
            allure.dynamic.parameter("endpoint",endpoint_id)
        allure.dynamic.parameter("case_name", case.get('name'))
        allure.dynamic.parameter("case_file", case.get("_case_file"))
        allure.dynamic.parameter("host", host_key)
        allure.dynamic.parameter("tags", case.get("tags",[]))
        allure.dynamic.parameter("order", case.get("order", 9999))
        allure.dynamic.parameter("depends_on", depends_on)

    @classmethod
    def build_header(cls,case:dict,admin_cookie:str)->dict:
    # ---------- 4. 认证与请求头 ----------

        auth = case.get("auth")
        headers = (case.get("headers", {}) or {}).copy()

        # 若 auth 为 "admin"，则将 admin_cookie 注入 Cookie 头
        # 注意：admin_cookie 应是字符串（如 "SESSION=xxx"），此处直接赋值
        if auth == "admin":
            headers['Cookie'] = admin_cookie
        return VariableResolver.resolve(headers)
    """
    增加--- effect 处理
    """

    @staticmethod
    def _effect_capture_enabled(case: dict) -> bool:
        effect = case.get("effect", {}) or {}
        capture = effect.get("capture", {}) or {}

        return bool(
            capture
            and capture.get("enabled", True) is not False
            and capture.get("before")
            and capture.get("after")
        )

    @classmethod
    def _build_effect_snapshot(cls,case: dict,before,after):
        effect_config = case.get("effect", {}) or {}
        target = effect_config.get("target", {}) or {}
        capture = effect_config.get("capture", {}) or {}

        return EffectCollector.build_snapshot(
            before=before,
            after=after,
            fields=target.get("fields", []) or [],
            selectors=capture.get("fields", {}) or {},
        )

    @staticmethod
    def _attach_effect_snapshot(case_id: str, snapshot: dict):
        allure.attach(
            json.dumps(
                # snapshot,
                SensitiveDataRedactor.redact(snapshot),
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            name=f"effect snapshot - {case_id}",
            attachment_type=allure.attachment_type.JSON,
        )

    @classmethod
    def _assert_effect_diff_contract(cls,case:dict,snapshot:dict):
        """v2.4.6A: 对更新契约实验做最小 Effect Diff 约束。

        required: 必须发生变化的逻辑字段。
        allowed: 允许发生变化的逻辑字段。

        这只是 Partial Update 专项的字段变化白名单，不扩展成通用
        State Contract DSL。
        """
        effect = case.get("effect", {}) or {}
        contract = effect.get("diff_contract", {}) or {}
        if not contract:
            return
        actual = set((snapshot.get("diff") or {}).keys())
        required = set(contract.get("required",[]) or [])
        allowed = set(contract.get("allowed",[]) or [])

        missing = sorted(required- actual)
        unexpected = sorted(actual- required)
        evidence = {
            "required": sorted(required),
            "allowed": sorted(allowed),
            "actual": sorted(actual),
            "missing": missing,
            "unexpected": unexpected,
        }
        allure.attach(
            json.dumps(evidence,ensure_ascii=False,indent=2),
            name = f"effect_diff_contract - {case.get('id')}",
            attachment_type=allure.attachment_type.JSON,
        )
        if missing or unexpected:
            raise AssertionError(
                f"{case.get('id')} Effect Diff Contract 失败: "
                f"missing={missing}, unexpected={unexpected}, "
                f"actual={sorted(actual)}"
            )

    @classmethod
    def _mark_effect_unknown(cls,case: dict,before,error):
        """
      在 before 已成功时，会先调用 EffectCollector.pick_fields
      将 before 原始 payload 转换为逻辑字段快照，
      这样即使主请求失败（如超时），rollback 仍能通过 restore_mapping 读取 before.xxx 完成恢复。

        """
        before_state = before
        if before is not None:
            effect_config = case.get("effect", {}) or {}
            target = effect_config.get("target", {}) or {}
            capture = effect_config.get("capture", {}) or {}
            fields = target.get("fields", []) or []
            selectors = capture.get("fields", {}) or {}

            if fields:
                before_state = EffectCollector.pick_fields(
                    before,
                    fields,
                    selectors,
                )
        snapshot = {
            "before": before_state,
            "after": None,
            "diff": {},
        }
        Context.mark_effect(
            case_id=case["id"],
            metadata=case.get("effect", {}),
            snapshot=snapshot,
            state=Context.EFFECT_UNKNOWN,
            error=error,
        )
        cls._attach_effect_snapshot(case["id"], snapshot)

    @classmethod
    def run(cls,case:dict,api_client_factory,admin_cookie:str,*,apply_metadata:bool = True,depends_on=None):
        """执行一条 Case，不处理跨 Case 依赖与 rollback 编排。"""
        # 获取当前case指定的Host YAML没写就默认走Host-be 写了就走指定的Host
        host_key = case.get("host", "Host-be")
        api_client = api_client_factory(host_key)

        if apply_metadata:
            cls.apply_allure_metadata(
                case,
                host_key,
                depends_on=depends_on,
            )
        # ---------- 5. 请求方法/路径 ----------
        method = case["method"]
        # path = case['path']
        # 增加类似 {{first_usg_ent_id}}替换变量
        path = VariableResolver.resolve(case["path"])

        # ---------- 6. 变量解析 ----------
        # 对 params、json、data 进行递归解析，替换 {{key}} 和 $timestamp_ms
        # headers 已在发送前完成变量解析
        params = VariableResolver.resolve(case.get("params", {}))
        headers = cls.build_header(case,admin_cookie)
        json_body = VariableResolver.resolve(case.get("json"))
        data = VariableResolver.resolve(case.get("data"))
        files = VariableResolver.resolve(case.get("files"))
        allow_redirects = case.get("allow_redirects", True)

        """
        增加变量修改前后变化
        """
        effect_enabled = cls._effect_capture_enabled(case)
        effect_before = None
        effect_snapshot = None
        # before Capture 失败时，主 destructive 请求不会发送。
        # 没有可恢复快照就拒绝修改，这是安全底线。
        if effect_enabled:
            effect_before = EffectExecutor.capture_before(
                case=case,
                api_client_factory=api_client_factory,
                admin_cookie=admin_cookie,
                header_builder=cls.build_header
            )
        # 运行前上下文快照
        # runtime_before = Context.snapshot()
        # ---------- 7. 发送请求 ----------
        # api_client.request 内部会自动添加 allure 步骤和附件
        try:
            response = api_client.request(
                method=method,
                path=path,
                params=params,
                headers=headers,  # headers  已经完成变量解析
                json=json_body,
                data=data,
                files=files,
                allow_redirects=allow_redirects,
            )
        except Exception as exc:
            # destructive 请求一旦开始发送，连接级异常无法证明服务端没有生效。
            # 保守标记 unknown，使后续 rollback 获得执行机会。
            if effect_enabled:
                cls._mark_effect_unknown(
                    case=case,
                    before=effect_before,
                    error=exc,
                )
            raise
        # destructive 请求发出后，先确认真实业务状态，再执行普通响应断言。
        # 即使后续断言失败，Effect 状态仍已记录，rollback 不会被 case_passed 拦住。
        if effect_enabled:
            try:
                effect_after = EffectExecutor.capture_after(
                    case=case,
                    api_client_factory=api_client_factory,
                    admin_cookie=admin_cookie,
                    header_builder=cls.build_header,
                )

                effect_snapshot = cls._build_effect_snapshot(
                    case=case,
                    before=effect_before,
                    after=effect_after,
                )

                effect_state = (
                    Context.EFFECT_APPLIED
                    if effect_snapshot["diff"]
                    else Context.EFFECT_NOT_APPLIED
                )

                Context.mark_effect(
                    case_id=case["id"],
                    metadata=case.get("effect", {}),
                    snapshot=effect_snapshot,
                    state=effect_state,
                )
                cls._attach_effect_snapshot(
                    case["id"],
                    effect_snapshot,
                )
            except Exception as exc:
                cls._mark_effect_unknown(
                    case=case,
                    before=effect_before,
                    error=exc,
                )
                raise

        # 使得 expected: "{{original_usr_display_bil}}" 能够被正确解析
        validations = VariableResolver.resolve(case.get("validate", []))

        # ---------- 8. 断言 ----------
        # 执行 YAML 中定义的 validate 列表
        Assertor.run_validations(response, validations)
        # update 类 Effect 通常要求确实发生变化。
        # 显式 require_change=true 时，零 diff 直接失败，彻底消灭“假绿”。
        if effect_enabled:
            effect_config = case.get("effect", {}) or {}
            if (
                    effect_config.get("require_change", False)
                    and Context.get_effect_state(case["id"])
                    == Context.EFFECT_NOT_APPLIED
                ):
                raise AssertionError(
                    f"{case['id']} Effect 未发生目标字段变化，"
                    "diff 为空。请检查测试前置值与目标值是否相同。"
                )
        # rollback 写 Case 响应断言通过，只记录“恢复请求已成功执行”。
        # Effect 状态仍保持 applied/unknown，直到恢复后 verify Case 真正通过。
        # if cleanup_depends:
        #     Context.mark_cleanup_targets(case_id=case["id"],effect_case_ids=cleanup_depends)
        # 增加动态变量提取
        extract_rules = VariableResolver.resolve(
            case.get("extract", {})
        )
        # ---------- 9. 提取变量 ----------
        # 执行 YAML 中定义的 extract 规则，结果自动存入 Context
        # extracted = Extractor.run_extractors(response, case.get("extract", {}))
        extracted = Extractor.run_extractors(response,extract_rules)
        #  增加附件，查看提取的变量数据

        if extracted:
            allure.attach(
                # str(extracted),
                # 提取变量信息脱敏-调试版本暂时注释
                str(SensitiveDataRedactor.redact(extracted)),
                name="extracted variables",
                attachment_type=allure.attachment_type.TEXT
            )
        # 恢复后验证通过，Effect 生命周期才真正进入 restored。
        # cls._mark_verified_restore(case)
        Context.mark_case_passed(case.get("id"))
        # 加入上下文内容至附件
        allure.attach(
            # str(Context.all()),
            # 上下文存取信息脱敏-调试版本暂时注释
            str(SensitiveDataRedactor.redact(Context.all())),
            name="context after effect",
            attachment_type=allure.attachment_type.TEXT
        )

        # ==============================
        # runtime context snapshot
        # ==============================

        allure.attach(
            # str(Context.snapshot()),
            # 上下文前后快照存取信息脱敏-调试版本暂时注释
            str(SensitiveDataRedactor.redact(Context.snapshot())),
            name="runtime context snapshot",
            attachment_type=allure.attachment_type.TEXT
        )

        # 增加 整条用例成功执行后，标记当前的case 已通过
        # ========== 10. 标记用例通过（供依赖检查） ==========
        # 只有完整执行（断言通过）后才标记，若断言失败则不会执行到此行
        # Context.set(f"case_passed.{case.get('id')}", True)
        # Context.mark_case_passed(case.get("id"))
        return response