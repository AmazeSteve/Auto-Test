"""
Effect Capture 执行器

v2.4.4B

职责：
1. Effect Capture 通过 Endpoint Registry 获取接口定义。
2. Capture 与普通 Case 复用同一套 host / auth / headers / VariableResolver。
3. 使用 ResponseParser 统一解析响应。
4. Capture 响应不可信时立即失败，禁止生成伪 Snapshot。
"""
import copy
import allure

from wizbank_api_test.core.case_normalizer import CaseNormalizer
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.core.response_parser import ResponseParser
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.utils.redaction import SensitiveDataRedactor

class EffectExecutor:
    """执行 destructive Case 的 before / after 状态采集。"""

    @classmethod
    def capture_before(cls,case:dict,api_client_factory,admin_cookie:str,header_builder):
      return cls._capture(
          case=case,
          phase='before',
          api_client_factory=api_client_factory,
          admin_cookie=admin_cookie,
          header_builder=header_builder,
      )

    @classmethod
    def capture_after(
            cls,
            case: dict,
            api_client_factory,
            admin_cookie: str,
            header_builder,
    ):
        return cls._capture(
            case=case,
            phase="after",
            api_client_factory=api_client_factory,
            admin_cookie=admin_cookie,
            header_builder=header_builder,
        )
    @classmethod
    def _capture(
        cls,
        case: dict,
        phase: str,
        api_client_factory,
        admin_cookie: str,
        header_builder,
    ):
        effect_config = case.get('effect', {}) or {}
        capture_config = effect_config.get("capture", {}) or {}

        if capture_config.get("enabled",True) is False :
            return {}
        phase_config = capture_config.get(phase)
        if not phase_config:
            return {}
        return cls.execute_capture(
            case=case,
            capture_config=phase_config,
            phase=phase,
            api_client_factory=api_client_factory,
            admin_cookie=admin_cookie,
            header_builder=header_builder,
        )
    @classmethod
    def execute_capture(cls, case, capture_config,phase, api_client_factory, admin_cookie, header_builder):

        """
        执行一次 Effect Capture。

        capture_config 只描述本次采集的业务参数，例如：

            endpoint: EP_USER_GET_FORM
            data:
              usr_ent_id: "{{target_usr_ent_id}}"
              pdate: $timestamp_ms
            expected_status: 200
            expected_code: 200

        endpoint 的 method / path / host / auth / headers 由 Registry 提供。
        capture_config 中显式字段仍可覆盖 endpoint 默认值。
        """
        endpoint_id = capture_config.get("endpoint")
        if not endpoint_id:
            raise ValueError(
                f"{case.get('id', 'UNKNOWN')} effect.capture.{phase} 缺少 endpoint"
            )

        registry = EndpointRegistry()
        endpoint = registry.get(endpoint_id)

        # 复用 CaseNormalizer 的 endpoint 合并规则，避免 EffectExecutor
        # 再维护一套 method/path/headers/auth 合并逻辑。
        capture_case = CaseNormalizer.merge_case_with_endpoint(
            capture_config,
            endpoint,
        )

        host_key = capture_case.get(
            "host",
            case.get("host", "Host-be"),
        )
        api_client = api_client_factory(host_key)

        method = str(capture_case["method"]).upper()
        path = VariableResolver.resolve(capture_case["path"])
        params = VariableResolver.resolve(capture_case.get("params", {}))
        headers = header_builder(capture_case, admin_cookie)
        json_body = VariableResolver.resolve(capture_case.get("json"))
        data = VariableResolver.resolve(capture_case.get("data"))
        files = VariableResolver.resolve(capture_case.get("files"))
        allow_redirects = capture_case.get("allow_redirects", True)

        with allure.step(
                f"Effect capture {phase}: {endpoint_id}"
        ):
            response = api_client.request(
                method=method,
                path=path,
                params=params,
                headers=headers,
                json=json_body,
                data=data,
                files=files,
                allow_redirects=allow_redirects,
            )
            # 将副作用影响Effect 响应结果获取
            payload = ResponseParser.parse_json(response)
            cls._validate_capture_response(
                response=response,
                payload=payload,
                capture_config=capture_config,
                case_id=case.get("id", "UNKNOWN"),
                phase=phase,
                endpoint_id=endpoint_id,
            )
            # 将副作用影响Effect 响应结果 挂载到报告中
            allure.attach(
                # str(payload),
                #  暂时注释脱敏，用于查阅调试结果
                str(SensitiveDataRedactor.redact(payload)),
                name=f"effect capture {phase} payload",
                attachment_type=allure.attachment_type.TEXT,
            )

        return copy.deepcopy(payload)

    @staticmethod
    def _validate_capture_response(
        response,
        payload,
        capture_config: dict,
        case_id: str,
        phase: str,
        endpoint_id: str,
    ):
        expected_status = capture_config.get("expected_status", 200)

        if response.status_code != expected_status:
            raise AssertionError(
                f"{case_id} effect.capture.{phase} HTTP 状态异常: "
                f"endpoint={endpoint_id}, "
                f"expected={expected_status}, actual={response.status_code}"
            )

        # 业务 code 不做全局猜测。需要检查时由 Capture Contract 显式声明，
        # 兼容不同系统 code=0 / 200 等成功语义。
        if "expected_code" in capture_config:
            expected_code = capture_config["expected_code"]

            if not isinstance(payload, dict):
                raise AssertionError(
                    f"{case_id} effect.capture.{phase} 无法校验业务 code: "
                    f"endpoint={endpoint_id}, payload_type={type(payload).__name__}"
                )

            actual_code = payload.get("code")
            if actual_code != expected_code:
                message = payload.get("message")
                raise AssertionError(
                    f"{case_id} effect.capture.{phase} 业务状态异常: "
                    f"endpoint={endpoint_id}, "
                    f"expected_code={expected_code}, actual_code={actual_code}, "
                    f"message={message}"
                )

