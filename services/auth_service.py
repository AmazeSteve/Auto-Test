"""Host/principal-bound WizBank login using the matching owned Session."""

import allure

from wizbank_api_test.core.client import assert_client_identity, normalize_principal
from wizbank_api_test.utils.encode_util import EncodeUtil


class AuthService:
    def __init__(self, api_client, config, *, host_key="Host-be", principal="admin"):
        principal = normalize_principal(principal)
        if principal == "none" or not isinstance(host_key, str) or not host_key:
            raise ValueError("Unsupported credential identity")
        self.api_client = api_client
        self.config = config
        self.host_key = host_key
        self.principal = principal

    @allure.step("admin 登录并获取 Cookie")
    def admin_login(self) -> str:
        return self._login("admin")

    @allure.step("学员登录并获得 Cookie")
    def learner_login(self) -> str:
        return self._login("learner")

    def _login(self, principal) -> str:
        if principal != self.principal:
            raise ValueError("Authentication service principal mismatch")
        assert_client_identity(self.api_client, self.host_key, principal)
        try:
            user = self.config.get_credential_user(self.host_key, principal)
            payload = {
                "eipCode": "",
                "usrSteUsrId": user["username"],
                "userPassword": EncodeUtil.base64_encode(user["password"]),
                "loginModeV2": "PC",
                "curLan": "zh-cn",
                "qrcodeType": "",
                "qrcodeItmType": "",
                "qrcodeencItmId": "",
            }
            response = self.api_client.post(
                "/app/user/login/v3",
                params={"developer": "PC"},
                json=payload,
                literal_json=True,
            )
            if response.status_code != 200:
                raise ValueError("Authentication login failed")
            cookie = response.headers.get("Set-Cookie")
            if not isinstance(cookie, str) or not cookie.strip():
                raise ValueError("Authentication login did not return a credential")
            return cookie
        except Exception:
            # Configuration/transport messages can contain raw credential material.
            raise ValueError("Authentication login failed") from None
