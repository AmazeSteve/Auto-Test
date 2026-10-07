import pytest
import allure
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.variable_resolver import VariableResolver

# """
# 暂时禁用 SSL
#
# """
# import urllib3
# urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

"""
简单登录
"""

@allure.feature("登录")
class TestLogin:
    @pytest.mark.smoke
    @pytest.mark.auth
    def test_admin_login(self,admin_cookie):
        assert admin_cookie
        assert  isinstance(admin_cookie, str)
