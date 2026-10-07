"""Allure / 日志敏感信息脱敏工具。"""

import copy


class SensitiveDataRedactor:
    """递归脱敏常见认证字段，不修改原对象。"""

    REDACTED = "<REDACTED>"

    SENSITIVE_EXACT_KEYS = {
        "cookie",
        "set_cookie",
        "authorization",
        "password",
        "userpassword",
        "usr_pwd",
        "pwd",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "api_key",
        "apikey",
        "secret",
        "admin_cookie",
    }

    @classmethod
    def _is_sensitive_key(cls, key) -> bool:
        normalized = str(key).strip().lower().replace("-", "_")

        if normalized in cls.SENSITIVE_EXACT_KEYS:
            return True

        if "cookie" in normalized:
            return True

        if normalized.endswith("_token"):
            return True

        if normalized.endswith("_password"):
            return True

        return False

    @classmethod
    def redact(cls, value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if cls._is_sensitive_key(key):
                    result[key] = cls.REDACTED
                else:
                    result[key] = cls.redact(item)
            return result

        if isinstance(value, list):
            return [cls.redact(item) for item in value]

        if isinstance(value, tuple):
            return tuple(cls.redact(item) for item in value)

        return copy.deepcopy(value)
