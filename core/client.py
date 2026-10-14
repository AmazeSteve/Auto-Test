"""

统一封装请求

"""
import inspect
import json
from copy import deepcopy
from http.cookies import CookieError, SimpleCookie


import requests
import allure
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.utils.redaction import SensitiveDataRedactor


def normalize_principal(principal):
    if principal is None:
        return "none"
    if not isinstance(principal, str) or principal not in ("admin", "learner", "none"):
        raise ValueError("Unsupported authentication principal")
    return principal


def runtime_principal(auth):
    return normalize_principal(auth)


def assert_client_identity(client, host_key, principal):
    """Authenticated credentials and transport must share an explicit identity."""
    principal = normalize_principal(principal)
    if (
        getattr(client, "host_key", None) != host_key
        or getattr(client, "principal", None) != principal
        or getattr(client, "session", None) is None
    ):
        raise ValueError("Authentication transport identity mismatch")


def resolve_runtime_credential(provider, client, host_key, principal):
    if principal == "none":
        return None
    from wizbank_api_test.services.credential_provider import Credential

    try:
        assert_client_identity(client, host_key, principal)
        provider.assert_client_matches(client, host_key, principal)
        credential = provider.get(host_key, principal)
        if not isinstance(credential, Credential):
            raise ValueError("Authenticated runtime requires a Credential")
        credential.assert_matches(host_key, principal)
        provider.assert_client_matches(client, host_key, principal)
        return credential
    except Exception:
        raise ValueError("Runtime credential identity or acquisition failed") from None


def reject_anonymous_auth_headers(headers):
    if any(str(name).casefold() in ("cookie", "authorization") for name in (headers or {})):
        raise ValueError("Anonymous requests cannot contain Cookie or Authorization headers")


def auth_secret_values(headers, session=None):
    """Known request/owned-Session secrets, used only at diagnostic boundaries."""
    values = set()
    for key, value in (headers or {}).items():
        if not isinstance(value, str) or not value:
            continue
        name = str(key).casefold()
        if name in ("cookie", "authorization"):
            values.add(value)
        if name == "cookie":
            cookies = SimpleCookie()
            try:
                cookies.load(value)
            except CookieError:
                pass
            values.update(item.value for item in cookies.values() if item.value)
        elif name == "authorization" and " " in value:
            values.add(value.split(" ", 1)[1])
    for cookie in getattr(session, "cookies", ()):
        value = getattr(cookie, "value", None)
        if isinstance(value, str) and value:
            values.add(value)
    return values


def redact_auth_evidence(value, sensitive_values=()):
    secrets = sorted(set(sensitive_values), key=len, reverse=True)

    def replace(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, SensitiveDataRedactor.REDACTED)
            return item
        if isinstance(item, dict):
            return {replace(key): replace(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return type(item)(replace(val) for val in item)
        return item

    return replace(SensitiveDataRedactor.redact(value))


def sanitize_auth_exception(error, sensitive_values=()):
    if not sensitive_values:
        return error
    pending, seen = [error], set()
    try:
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            current.args = tuple(redact_auth_evidence(arg, sensitive_values) for arg in current.args)
            if any(value in str(current) for value in sensitive_values):
                return ValueError("Authenticated request processing failed")
            pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
    except Exception:
        return ValueError("Authenticated request processing failed")
    return error


def get_principal_client(api_client_factory, host_key, principal):
    """Route explicitly; retain only legacy one-argument anonymous factories."""
    principal = normalize_principal(principal)
    if principal == "none":
        try:
            signature = inspect.signature(api_client_factory)
        except (TypeError, ValueError):
            pass
        else:
            try:
                signature.bind(host_key, principal=principal)
            except TypeError:
                if len(signature.parameters) != 1:
                    raise
                parameter = next(iter(signature.parameters.values()))
                if parameter.kind not in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    raise
                signature.bind(host_key)
                return api_client_factory(host_key)
    return api_client_factory(host_key, principal=principal)


class _PrincipalAuth(requests.auth.AuthBase):
    """Explicit auth ownership without deriving credentials from ambient NETRC."""

    def __call__(self, request):
        return request


class _PrincipalSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.auth = _PrincipalAuth()

    def rebuild_auth(self, prepared_request, response):
        # Preserve Requests' cross-origin stripping without its redirect NETRC lookup.
        if "Authorization" in prepared_request.headers and self.should_strip_auth(
            response.request.url, prepared_request.url
        ):
            del prepared_request.headers["Authorization"]

"""
数据流向：

测试用例在 fixture 或前置操作中将动态数据（如登录后的 token）存入 Context。
测试数据（YAML/JSON）中写占位符，如 "Authorization": "Bearer {{token}}"。
调用 ApiClient.post() 时，VariableResolver 自动从 Context 中取出 token 并替换。
最终请求携带真实值，实现接口依赖数据的自动传递。
"""
class ApiClient:
    """增加信息脱敏"""
    SENSITIVE_KEYS = {
            "cookie",
            "set-cookie",
            "authorization",
            "password",
            "userpassword",
            "token",
            "access_token",
            "refresh_token",
        }
    def __init__(self,base_url:str,default_headers=None,timeout=30,verify_ssl=False,*,principal=None,host_key=None):
        # Existing direct construction remains compatible; factory clients are explicit.
        self.principal = normalize_principal(principal) if principal is not None else None
        self.host_key = host_key
        if self.principal == "none":
            reject_anonymous_auth_headers(default_headers)
        self.base_url = base_url.rstrip('/')
        self.session = _PrincipalSession() if self.principal is not None else requests.Session()
        self.default_headers = default_headers or {}
        self.timeout = timeout
        self.verify_ssl = verify_ssl

    def request(
        self,
        method: str,
        path: str,
        params=None,
        headers=None,
        json=None,
        data=None,
        files=None,
        allow_redirects=True,
        literal_json=False,
    ):
        url = self.base_url +"/" +path.lstrip("/")
        if self.principal == "none":
            reject_anonymous_auth_headers(self.default_headers)
            reject_anonymous_auth_headers(headers)

        # 给到变量解释器迭代获取数据
        resolved_params = VariableResolver.resolve(params or {})
        request_headers = headers or {}
        literal_cookies = {
            key: deepcopy(value) for key, value in request_headers.items()
            if self.principal in ("admin", "learner") and str(key).casefold() == "cookie"
        }
        resolved_headers = VariableResolver.resolve({
            key: value for key, value in request_headers.items() if key not in literal_cookies
        })
        resolved_headers.update(literal_cookies)
        # 避免乱传
        resolved_json = (
            (deepcopy(json) if literal_json else VariableResolver.resolve(json))
            if json is not None
            else None
        )

        resolved_data = (
            VariableResolver.resolve(data)
            if data is not None
            else None
        )

        final_headers = {}
        final_headers.update(self.default_headers)
        final_headers.update(resolved_headers)
        sensitive_values = auth_secret_values(final_headers, self.session)

#         通用allure
        """
        Allure 报告记录
        使用 with allure.step() 将请求包装为一个测试步骤，在报告中清晰展示。
        通过 allure.attach 分别记录请求详情（URL、方法、参数、头部、Body）和响应内容（状态码 + 前 5000 字符，防止过大）
        
        """
        """脱敏工具调用-调式阶段暂时保留部分信息显示"""
        login_request = path.lstrip("/").split("?", 1)[0] == "app/user/login/v3"
        request_evidence= {
            "url": url,
            "method": method,
            "params": resolved_params,
            "headers": final_headers,
            "json": resolved_json,
            "data": resolved_data,
        }
        if login_request:
            request_evidence = {"host_key": self.host_key, "principal": self.principal}
        with allure.step(f"{method.upper()} {path}"):
            # 将解释的数据作为附件写入
            allure.attach(
                str(
                    # {
                    #     "url":url,
                    #     "method":method,
                    #     "params":resolved_params,
                    #     "headers":resolved_headers,
                    #     "json":resolved_json,
                    #     "data":resolved_data,
                    #     脱敏信息 需要用法 上述暂时保持用于调试
                    #     "params": self._mask_sensitive(resolved_params),
                    #     "headers": self._mask_sensitive(final_headers),
                    #     "json": self._mask_sensitive(resolved_json),
                    #     "data": self._mask_sensitive(resolved_data),
                    # }
                #     脱敏工具使用-暂时注释-方便调试
                    redact_auth_evidence(request_evidence, sensitive_values)
                ),
                name="request",
                attachment_type=allure.attachment_type.TEXT,
            )
        # 发起请求
        try:
            response = self.session.request(
                method=method, url=url, params=resolved_params, headers=final_headers,
                json=resolved_json, data=resolved_data, files=files,
                timeout=self.timeout, verify=self.verify_ssl, allow_redirects=allow_redirects,
            )
        except Exception as exc:
            sensitive_values.update(auth_secret_values(final_headers, self.session))
            safe_error = sanitize_auth_exception(exc, sensitive_values)
            if safe_error is exc:
                raise
            raise safe_error from None
        # # 响应结果脱敏 现在暂时不使用 保持调试状态
        sensitive_values.update(auth_secret_values(final_headers, self.session))
        safe_response = None if login_request else self._safe_response_text(response, sensitive_values=sensitive_values)

        allure.attach(
            # f"status_code:{response.status_code} \n\n{response.text[:5000]}",
            # 响应结果脱敏 现在暂时不使用 保持调试状态
            f"status_code:{response.status_code}" if login_request else f"status_code:{response.status_code} \n\n{safe_response}",
            name="response",
            attachment_type=allure.attachment_type.TEXT,
        )

        return response
    # GET请求
    def get(self,path:str,**kwargs):
        return self.request("GET",path,**kwargs)

    def post(self,path:str,**kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)
    # 信息脱敏内置
    # @classmethod
    # def _mask_sensitive(cls, value):
    #     if isinstance(value, dict):
    #         result = {}
    #
    #         for key, item in value.items():
    #             normalized_key = str(key).lower()
    #
    #             if normalized_key in cls.SENSITIVE_KEYS:
    #                 result[key] = "<REDACTED>"
    #             else:
    #                 result[key] = cls._mask_sensitive(item)
    #
    #         return result
    #
    #     if isinstance(value, list):
    #         return [
    #             cls._mask_sensitive(item)
    #             for item in value
    #         ]
    #
    #     return value
    @classmethod
    def _mask_sensitive(cls, value):
        """保留旧调用接口，统一委托 SensitiveDataRedactor。"""
        return SensitiveDataRedactor.redact(value)
    # 信息脱敏内置-响应JSON
    @classmethod
    def _safe_response_text(cls, response,max_length=5000,*,sensitive_values=()):
        try:
            payload = response.json()
            masked_payload = redact_auth_evidence(payload, sensitive_values)
            text = json.dumps(
                masked_payload,
                ensure_ascii=False,
                indent=2,
            )
            return text[:max_length]
        except ValueError:
            return redact_auth_evidence(response.text or "", sensitive_values)[:max_length]
