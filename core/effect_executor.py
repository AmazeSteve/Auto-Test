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
from wizbank_api_test.core.client import (
    get_principal_client, reject_anonymous_auth_headers, runtime_principal,
    resolve_runtime_credential,
    auth_secret_values, redact_auth_evidence, sanitize_auth_exception,
)
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.core.response_parser import ResponseParser
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.utils.redaction import SensitiveDataRedactor

class EffectExecutor:
    """执行 destructive Case 的 before / after 状态采集。"""

    @classmethod
    def capture_before(cls,case:dict,api_client_factory,credential_provider,header_builder):
      return cls._capture(
          case=case,
          phase='before',
          api_client_factory=api_client_factory,
          credential_provider=credential_provider,
          header_builder=header_builder,
      )

    @classmethod
    def capture_after(
            cls,
            case: dict,
            api_client_factory,
            credential_provider,
            header_builder,
    ):
        return cls._capture(
            case=case,
            phase="after",
            api_client_factory=api_client_factory,
            credential_provider=credential_provider,
            header_builder=header_builder,
        )
    @classmethod
    def _capture(
        cls,
        case: dict,
        phase: str,
        api_client_factory,
        credential_provider,
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
            credential_provider=credential_provider,
            header_builder=header_builder,
        )
    @classmethod
    def execute_capture(cls, case, capture_config,phase, api_client_factory, credential_provider, header_builder):

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
        principal = runtime_principal(capture_case.get("auth"))
        api_client = get_principal_client(api_client_factory, host_key, principal)
        credential = resolve_runtime_credential(credential_provider, api_client, host_key, principal)

        method = str(capture_case["method"]).upper()
        path = VariableResolver.resolve(capture_case["path"])
        params = VariableResolver.resolve(capture_case.get("params", {}))
        headers = header_builder({**capture_case, "host": host_key}, credential)
        if principal == "none":
            reject_anonymous_auth_headers(headers)
        json_body = VariableResolver.resolve(capture_case.get("json"))
        data = VariableResolver.resolve(capture_case.get("data"))
        files = VariableResolver.resolve(capture_case.get("files"))
        allow_redirects = capture_case.get("allow_redirects", True)
        evidence_headers = {**getattr(api_client, "default_headers", {}), **headers}
        sensitive_values = auth_secret_values(evidence_headers, getattr(api_client, "session", None))

        with allure.step(
                f"Effect capture {phase}: {endpoint_id}"
        ):
            try:
                response = api_client.request(
                    method=method, path=path, params=params, headers=headers,
                    json=json_body, data=data, files=files, allow_redirects=allow_redirects,
                )
                sensitive_values.update(auth_secret_values(evidence_headers, getattr(api_client, "session", None)))
                payload = ResponseParser.parse_json(response)
                cls._validate_capture_response(
                    response=response, payload=payload, capture_config=capture_config,
                    case_id=case.get("id", "UNKNOWN"), phase=phase, endpoint_id=endpoint_id,
                )
                allure.attach(
                    str(redact_auth_evidence(payload, sensitive_values)),
                    name=f"effect capture {phase} payload",
                    attachment_type=allure.attachment_type.TEXT,
                )
            except Exception as exc:
                sensitive_values.update(auth_secret_values(evidence_headers, getattr(api_client, "session", None)))
                safe_error = sanitize_auth_exception(exc, sensitive_values)
                if safe_error is exc:
                    raise
                raise safe_error from None

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

