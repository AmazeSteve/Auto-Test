import allure

from wizbank_api_test.utils.encode_util  import EncodeUtil

"""
处理授权逻辑
"""
class AuthService(object):
    def __init__(self, api_client,config):
        self.api_client = api_client
        self.config = config

    @allure.step("admin 登录并获取 Cookie")
    def admin_login(self)->str:
        user = self.config.get_user("admin")
        payload = {
        "eipCode": "",
        "usrSteUsrId": user['username'],
        "userPassword": EncodeUtil.base64_encode(user['password']),
        "loginModeV2": "PC",
        "curLan": "zh-cn",
        "qrcodeType": "",
        "qrcodeItmType": "",
        "qrcodeencItmId": ""
        }

        response = self.api_client.post(
            '/app/user/login/v3',
            params={"developer":"PC"},
            json=payload
        )

        assert response.status_code == 200 , f"admin 登录失败，状态码:{response.status_code}"

        cookie = response.headers.get('Set-Cookie');

        assert cookie, "admin 登录响应未获得到 Set-Cookie"
        return cookie

    @allure.step("学员登录并获得 Cookie")
    def learner_login(self)->str:
        user = self.config.get_user("learner")

        payload = {
            "eipCode": "",
            "usrSteUsrId": user['username'],
            "userPassword": EncodeUtil.base64_encode(user['password']),
            "loginModeV2": "PC",
            "curLan": "zh-cn",
            "qrcodeType": "",
            "qrcodeItmType": "",
            "qrcodeencItmId": ""
        }

        response = self.api_client.post(
            '/app/user/login/v3',
            params={"developer":"PC"},
            json=payload
        )

        assert response.status_code == 200, f"学员 登录失败，状态码:{response.status_code}"

        cookie = response.headers.get('Set-Cookie');

        assert cookie, "学员 登录响应未获得到 Set-Cookie"
        return cookie