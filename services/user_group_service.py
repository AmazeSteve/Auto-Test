import allure
from wizbank_api_test.core.variable_resolver import VariableResolver


class UserGroupService():

    def __init__(self, api_client,admin_cookie:str):
        """

        :param api_client:  共用处理Request 夹具
        :param admin_cookie:  管理员令牌
        """
        self.api_client = api_client
        self.admin_cookie = admin_cookie

    # 响应管理员令牌
    def _headers(self)->dict:
        return {
            "Cookie":self.admin_cookie,
        }

    @allure.step("查询首级用户组")
    def query_first_level_groups(self):
        return self.api_client.get(
            "/app/v2/admin/user/user_group/0",
            headers=self._headers(),
            params={
                "pdate": VariableResolver.timestamp_ms()
            }
        )

    @allure.step("查询首级用户组树")
    def query_group_tree(self):
        return self.api_client.get(
            "/app/v2/admin/user/user_group/plugin/tree",
            headers=self._headers(),
            params={
                "pdate": VariableResolver.timestamp_ms()
            }
        )

    @allure.step("查询指定用户组详情")
    def query_group_detail(self,usg_ent_id:str):
        return self.api_client.get(
            f"/app/v2/admin/user/user_group/detail/{usg_ent_id}",
            headers=self._headers(),
            params={
                "pdate": VariableResolver.timestamp_ms()
            }
        )

    @allure.step("查询指定用户组下用户列表")
    def query_group_users(self, usg_ent_id: str,page_num:int=1,page_size:int=10):
        return self.api_client.get(
            f"/app/v2/admin/user/user_group/user/{usg_ent_id}",
            headers=self._headers(),
            params={
                "pageNum": page_num,
                "pageSize": page_size,
                "sortField": "usr_upd_date",
                "sortMode": "desc",
                "pdate": VariableResolver.timestamp_ms()

            }
        )



