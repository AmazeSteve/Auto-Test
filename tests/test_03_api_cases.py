from pathlib import Path

import  pytest

from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.case_selector import  CaseSelector
from wizbank_api_test.core.case_validator import CaseValidator
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner

CASE_DIR = Path(__file__).resolve().parents[1] /"data"/"cases"
ENDPOINT_DIR = Path(__file__).resolve().parents[1] /"data"/"endpoints"
#  全局变量：在模块加载时一次性加载所有用例，避免 pytest 收集阶段重复读取
ALL_CASES = CaseLoader.load_cases(CASE_DIR,ENDPOINT_DIR)
# 增加用例校验：在测试收集阶段即检查所有 YAML 用例是否合法
# 如果校验失败，pytest 会直接报错并终止，不会执行任何测试
CaseValidator.validate_all(ALL_CASES)

# 构建 ID -> 用例 的映射，方便快速查找
CASE_MAP = {
    case["id"]: case
    for case in ALL_CASES
}

# 所有用例 ID 列表（用于参数化，保证执行顺序与 ALL_CASES 一致）
CASE_IDS = [
    case["id"]
    for case in ALL_CASES]

LIFECYCLE_CHILD_MAP = LifecycleRunner.lifecycle_child_map(ALL_CASES)
"""

数据驱动接口测试执行器

职责：
1. 自动加载 data/cases/ 目录下所有 .yaml 文件中的测试用例
2. 将每个用例转换为一个独立的 pytest 参数化测试
3. 动态应用 Allure 装饰（feature/story/title）
4. 支持 --tag 命令行过滤，只运行包含指定 tag 的用例
5. 支持多 Host（通过 api_client_factory）
6. 执行请求前解析变量（VariableResolver），注入认证信息
7. 执行断言（Assertor）和提取（Extractor）
8. 支持 tags 控制（如 destructive 用例条件跳过）

使用方式：
    pytest --env local --alluredir=reports/allure-results
    或通过 -m smoke 只运行带有 smoke 标签的用例（需在 pytest.ini 配置）

"""
# 收集
class TestApiCases:
    @pytest.mark.parametrize(
        "case_id",
        CASE_IDS,
        ids=CASE_IDS
    )
    def test_api_case(self,api_client_factory,admin_cookie,request,case_id):
        """统一 YAML Case 入口。

        普通 Case：LifecycleRunner.run_case -> CaseRunner.run
        Lifecycle root：LifecycleRunner.run_lifecycle 在同一个 pytest item 内执行
        modify / verify / rollback / verify_rollback。

        lifecycle managed child 保留在参数化列表中用于兼容和可见性，但独立 item
        不再发送请求，统一由 root 管理。
        """


        case = CASE_MAP[case_id]



        # 增加命令行标签过滤处理 如果使用--tag 说明指定标记用例
        target_tag = request.config.getoption("--tag")

        if not CaseSelector.should_run_by_tag(case,ALL_CASES,target_tag):
            pytest.skip(f"当前用例不包含 tag:{target_tag},且不属于 目标依赖，跳过")

        # CaseRunner.run(
        #     case = case,
        #     api_client_factory = api_client_factory,
        #     admin_cookie = admin_cookie,
        #     request = request
        # )
        managed_by = LIFECYCLE_CHILD_MAP.get(case_id)
        # 新增：managed child 跳过
        if managed_by:
            pytest.skip( f"当前用例由 lifecycle root {managed_by} 统一编排，跳过独立执行")
            # 新增：lifecycle root 走生命周期编排
        if case.get("lifecycle"):
            LifecycleRunner.run_lifecycle(
                root_case=case,
                case_map=CASE_MAP,
                api_client_factory=api_client_factory,
                admin_cookie=admin_cookie,
                request=request,
            )
            return
        # 普通 Case 走单 Case 执行
        LifecycleRunner.run_case(
            case=case,
            api_client_factory=api_client_factory,
            admin_cookie=admin_cookie,
            request=request,
        )