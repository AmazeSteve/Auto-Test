"""
v1.4 做运行入口与报告清理封装

执行全部测试
python scripts/run_api.py --env local --clean
只执行 YAML 用例
python scripts/run_api.py --env local --yaml-only --clean
只执行 YAML smoke
python scripts/run_api.py --env local --yaml-only --tag smoke --clean
只执行 YAML dependency，并自动带依赖
python scripts/run_api.py --env local --yaml-only --tag dependency --clean
查看报告
allure serve reports/allure-results

"""

"""

"""