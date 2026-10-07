import pytest
from wizbank_api_test.core.config import  Config
from wizbank_api_test.core.client import  ApiClient
from wizbank_api_test.core.context import Context
from wizbank_api_test.services.auth_service import AuthService
from wizbank_api_test.services.user_group_service import UserGroupService

"""
钩子 - 自定义命令
"""
def pytest_addoption(parser):
    parser.addoption(
        '--env',
        action='store',
        default='local',
        help='指定测试环境,例如local/test/dev'
    )
    parser.addoption(
        '--allow-destructive',
        action='store_true',
        default=False,
        help='允许执行 destructive 用例'
    )
    parser.addoption(
        '--allow-contract-experiment',
        action='store_true',
        default=False,
        help='允许执行接口契约实验用例；必须同时显式允许 destructive'
    )
#     增加--tag 过滤
    parser.addoption(
    '--tag',
    action='store',
    default=None,
    help="只执行包含指定 tag的YAML 用例，例如 --tag smoke"
    )
# 会话级别夹具
@pytest.fixture(scope='session')
def config(request):
        env = request.config.getoption('--env')
        return Config(env)

#支持多个HOST 环境
@pytest.fixture(scope='session')
def api_client_factory(config):
    """
    api_client_factory("Host-be")  → WizBank 管理端
    api_client_factory("Host-156") → LyndonAI
    api_client_factory("Host-226") → 指定旧环境

    多 Host 客户端工厂（会话级）

    用途：
        根据不同的 host_key 创建/复用对应的 ApiClient 实例。
        避免为每个用例重复创建客户端，同时支持同一测试中访问不同后端服务。

    使用方式：
        在测试用例中调用 api_client_factory("Host-be") 获取 WizBank 管理端客户端，
        或 api_client_factory("Host-156") 获取 LyndonAI 客户端等。

    参数：
        config: 环境配置对象，提供不同 Host 的 base_url

    返回：
        _get_client 函数，该函数接收 host_key 并返回对应的 ApiClient 实例
    """
    clients = {} # 缓存已创建的客户端，key 为 host_key

    def _get_client(host_key:str = "Host-be"):
        """内部工厂函数，按需创建并缓存客户端"""
        if host_key not in clients:
            base_url = config.get_base_url(host_key)

            clients[host_key] = ApiClient(
                base_url=base_url,
                default_headers=config.get_default_headers(),
                timeout=config.get_timeout(),
                verify_ssl=config.get_verify_ssl(),
            )
        return clients[host_key]
    return _get_client
"""
共用发起API夹具
"""
@pytest.fixture(scope='session')
def api_client(api_client_factory):
        # base_url = config.get_base_url("Host-be")
        #
        # return ApiClient(
        #     base_url=base_url,
        #     default_headers=config.get_default_headers(),
        #     timeout=config.get_timeout(),
        #     verify_ssl=config.get_verify_ssl(),
        # )
#         调整支持多HOST
        """
        默认 API 客户端（向后兼容）

        保持原有 fixture 名称，便于旧测试用例直接使用。
        内部调用 api_client_factory("Host-be") 返回默认 Host 的客户端。
        """
        return api_client_factory("Host-be")
# 登录对象初始化
@pytest.fixture(scope='session')
def auth_service(api_client,config):
    return AuthService(api_client, config)

@pytest.fixture(scope='session')
def admin_cookie(auth_service):
#     cookie = auth_service.admin_login()
# #     保存在上下文内容
#     Context.set('admin_cookie', cookie)
#     # 增加多一个变量保存 cookie
#     Context.set('admin_Cookie', cookie)
#     return cookie
    # Cookie 只由 fixture 持有并显式传给请求层。
    # v2.4.5A 起不再写入 Context，避免运行时状态与报告中残留会话凭证。
    return auth_service.admin_login()


@pytest.fixture(scope='session')
def user_group_service(api_client,admin_cookie):
    """

    :param api_client:  共用API Client
    :param admin_cookie:  全局会话admin_cookie 夹具
    :return:
    """
    return UserGroupService(api_client, admin_cookie)

