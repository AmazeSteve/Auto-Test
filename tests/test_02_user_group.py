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

@allure.feature('用户管理')
@allure.story('用户组')
class TestUserGroup:
    @pytest.mark.readonly
    @pytest.mark.smoke
    def test_query_first_level_user_group(self,user_group_service):

        # 令牌情况夹具会处理
        response = user_group_service.query_first_level_groups()
        assert response.status_code == 200

        json_data = response.json()

        assert  "data" in json_data
        user_group_list = json_data.get("data",{}).get("userGroupList",[])

    @pytest.mark.readonly
    def test_query_user_group_tree(self,user_group_service):
        # 用户组树形结构
        response = user_group_service.query_group_tree()
        assert response.status_code == 200

        json_data = response.json()
        assert "data" in json_data


