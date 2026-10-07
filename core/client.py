"""

统一封装请求

"""
import json


import requests
import allure
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.utils.redaction import SensitiveDataRedactor

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
    def __init__(self,base_url:str,default_headers=None,timeout=30,verify_ssl=False):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
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
    ):
        url = self.base_url +"/" +path.lstrip("/")

        # 给到变量解释器迭代获取数据
        resolved_params = VariableResolver.resolve(params or {})
        resolved_headers = VariableResolver.resolve(headers or {})
        # 避免乱传
        resolved_json = (
            VariableResolver.resolve(json)
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

#         通用allure
        """
        Allure 报告记录
        使用 with allure.step() 将请求包装为一个测试步骤，在报告中清晰展示。
        通过 allure.attach 分别记录请求详情（URL、方法、参数、头部、Body）和响应内容（状态码 + 前 5000 字符，防止过大）
        
        """
        """脱敏工具调用-调式阶段暂时保留部分信息显示"""
        request_evidence= {
            "url": url,
            "method": method,
            "params": resolved_params,
            "headers": final_headers,
            "json": resolved_json,
            "data": resolved_data,
        }
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
                    SensitiveDataRedactor.redact(request_evidence)
                ),
                name="request",
                attachment_type=allure.attachment_type.TEXT,
            )
        # 发起请求
        response =  self.session.request(
            method = method,
            url = url,
            params=resolved_params,
            headers = final_headers,
            json=resolved_json,
            data=resolved_data,
            files=files,
            timeout=self.timeout,
            verify=self.verify_ssl,
            allow_redirects=allow_redirects,
        )
        # # 响应结果脱敏 现在暂时不使用 保持调试状态
        safe_response = self._safe_response_text(response)

        allure.attach(
            # f"status_code:{response.status_code} \n\n{response.text[:5000]}",
            # 响应结果脱敏 现在暂时不使用 保持调试状态
            f"status_code:{response.status_code} \n\n{safe_response}",
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
    def _safe_response_text(cls, response,max_length=5000):
        try:
            payload = response.json()
            masked_payload = SensitiveDataRedactor.redact(payload)
            text = json.dumps(
                masked_payload,
                ensure_ascii=False,
                indent=2,
            )
            return text[:max_length]
        except ValueError:
            return (response.text or "")[:max_length]
