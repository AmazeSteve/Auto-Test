"""

v0.2 调整
1. 任何用例需要 admin_cookie，就自动登录。
2. 不依赖 test_01_login.py 先执行。
3. Context 仍然保留，后面跨步骤变量还可以用。
4. service 层负责接口细节。
5. tests 层只负责验证。

v0.3

Yaml 提取
准备阶段
conftest.py 提供 api_client 和 admin_cookie 夹具（admin_cookie 由 AuthService 登录生成，并存入 Context）。
测试启动时，load_cases() 递归读取 data/cases/*.yaml，生成用例列表。

执行阶段（每个用例）
变量解析：VariableResolver 将 params/json/data 中的 {{key}} 替换为 Context 中的值（如 admin_cookie、user_grp_list）。
发送请求：ApiClient.request 自动记录 Allure 步骤和请求/响应详情。
断言：Assertor 按 validate 规则校验响应。
提取：Extractor 从响应中按 extract 规则提取数据，并存入 Context 供后续用例使用。
报告生成
Allure 动态装饰将用例按 module/name/id 组织，清晰呈现每个用例的执行结果和请求响应详情
"""

"""
v0.6
全量运行 
pytest --env local --alluredir=reports/allure-results
指定标记 smoke 自定义
pytest --env local --tag smoke  --alluredir=reports/allure-results
指定标记 readonly 自定义
pytest --env local --tag readonly  --alluredir=reports/allure-results
如果需要全体用例受控 使用pytest 标记在收集阶段过滤
pytest --env local  -m smoke --alluredir=reports/allure-results
只是YAML 用例受控当前是
pytest tests/test_03_api_cases.py --env local --tag smoke  --alluredir=reports/allure-results
"""

"""
v0.9
新增功能：
    1. 用例执行顺序控制：通过 order 字段升序执行
    2. 用例依赖管理：通过 depends_on 字段指定依赖的用例 ID，
       执行前检查依赖用例是否已通过，若未通过则跳过当前用例
    3. 用例通过标记：执行成功后写入 Context，供依赖检查使用
pytest --env local --alluredir=reports/allure-results 全量
只跑动前后依赖 dependency
pytest tests/test_03_api_cases.py --env local --tag dependency --alluredir=reports/allure-results

只跑 readonly
pytest tests/test_03_api_cases.py --env local --tag readonly --alluredir=reports/allure-results
"""

"""
v1.0 做「tag 过滤自动带上依赖」
使得 --tag 过滤时能够自动包含所有前置依赖用例，避免因缺少依赖数据导致用例跳过。这是一个非常实用的增强，尤其适用于复杂业务场景
新增功能：
    1. 自动加载 data/cases/ 下的 YAML 用例
    2. 支持 order 排序，确保依赖用例先执行
    3. 支持 depends_on 依赖管理，自动检查前置用例是否通过
    4. 支持 --tag 过滤，并自动包含目标用例的完整依赖链
    5. 多 Host 支持、Allure 动态装饰、变量解析、断言与提取
    全量执行 pytest --env local --alluredir=reports/allure-results
    只跑dependency
    pytest tests/test_03_api_cases.py --env local --tag dependency --alluredir=reports/allure-results
    只跑 smoke
    pytest tests/test_03_api_cases.py --env local --tag smoke --alluredir=reports/allure-results
"""

"""
v1.1 启动测试前，先检查所有 YAML 用例结构是否健康。
1. id 是否存在
2. id 是否重复
3. name 是否存在
4. method 是否存在
5. path 是否存在
6. method 是否属于 GET/POST/PUT/PATCH/DELETE
7. depends_on 引用的 case 是否存在
8. 是否存在自依赖
9. 是否存在循环依赖
10. priority 是否属于 P0/P1/P2/P3/P4
11. tags 是否为 list
12. order 是否为 int
"""
"""
v1.3 分离 test_03_api 
pytest tests/test_03_api_cases.py --env local --alluredir=reports/allure-results
"""