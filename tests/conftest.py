import pytest
from wizbank_api_test.core.config import  Config
from wizbank_api_test.core.client import ApiClient, normalize_principal
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
    parser.addoption(
        '--allow-c1-real-lifecycle',
        action='store_true',
        default=False,
        help='C1 真实生命周期额外许可；还需 destructive 许可及显式双重用户名确认'
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
    """按 Host 和身份隔离 Session；匿名调用每次返回新客户端。"""
    clients = {}

    def _get_client(host_key: str = "Host-be", principal="none"):
        principal = normalize_principal(principal)
        key = (host_key, principal)
        if principal == "none" or key not in clients:
            client = ApiClient(
                base_url=config.get_base_url(host_key),
                default_headers=config.get_default_headers(),
                timeout=config.get_timeout(),
                verify_ssl=config.get_verify_ssl(),
                principal=principal,
            )
            if principal == "none":
                return client
            clients[key] = client
        return clients[key]
    return _get_client
"""
共用发起API夹具
"""
@pytest.fixture(scope='function')
def api_client(api_client_factory):
    """保留默认匿名客户端 fixture，Session 不跨测试项复用。"""
    return api_client_factory("Host-be", "none")
# 登录对象初始化
@pytest.fixture(scope='session')
def auth_service(api_client_factory,config):
    return AuthService(api_client_factory("Host-be", "admin"), config)

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
def user_group_service(admin_cookie,api_client_factory):
    """

    :param api_client_factory:  按 Host 和身份隔离的客户端工厂
    :param admin_cookie:  全局会话admin_cookie 夹具
    :return:
    """
    return UserGroupService(api_client_factory("Host-be", "admin"), admin_cookie)

