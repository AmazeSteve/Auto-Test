import  argparse
import shutil
import subprocess

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ALLURE_RESULTS = ROOT_DIR / "reports" /"allure-results"

def clean_allure_results():
    if ALLURE_RESULTS.exists():
        shutil.rmtree(ALLURE_RESULTS)

    ALLURE_RESULTS.mkdir(parents=True,exist_ok=True)

def build_pytest_command(env:str,tag:str | None,yaml_only:bool,allow_destructive:bool,allow_contract_experiment:bool=False,update_contract:bool=False):

    """
    构建 pytest 命令行参数列表

    参数：
        env: 测试环境（local/test/dev）
        tag: 用例标签过滤（如 smoke）
        yaml_only: 是否只执行 YAML 驱动用例
        clean: 是否添加 --clean-alluredir 参数（pytest 插件参数）
        allow_destructive:是否执行YAML 危险用例
    返回：
        list: 完整的命令参数列表
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
    ]
    if update_contract:
        cmd.append("tests/test_04_user_update_contract.py")
    elif yaml_only:
        cmd.append("tests/test_03_api_cases.py")

    cmd.extend(
        ["--env",
         env,
         f"--alluredir={ALLURE_RESULTS}",
        ])
    if tag:
        cmd.extend(["--tag", tag])
    if allow_destructive:
        cmd.extend(["--allow-destructive"])
    if allow_contract_experiment:
        cmd.extend(["--allow-contract-experiment"])
    return cmd
def main():
    """解析命令行参数并执行测试"""
    parser = argparse.ArgumentParser(description="WizBank API 自动化测试运行入口 ")

    parser.add_argument(
        '--env',
        default="local",
        help="测试环境，例如 local/test/dev"
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="YAML 用例标签过滤，例如 smoke/readonly/dependency"
    )

    parser.add_argument(
        "--yaml-only",
        action="store_true",
        help="只执行 YAML 数据驱动接口用例"
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="执行前清理 Allure 结果目录"
    )

    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="允许执行 destructive 危险用例"
    )
    parser.add_argument(
        "--allow-contract-experiment",
        action="store_true",
        help="允许执行接口更新契约实验；必须与 --allow-destructive 同时使用"
    )
    parser.add_argument(
        "--update-contract",
        action="store_true",
        help="只执行 /upd_user Partial Update 契约专项"
    )
    args = parser.parse_args()
    if args.update_contract:
        if not args.allow_destructive or not args.allow_contract_experiment:
            parser.error(
                "--update-contract 必须同时指定 --allow-destructive 和 "
                "--allow-contract-experiment"
            )
        args.tag = "update_contract"
    cmd = build_pytest_command(
        env=args.env,
        tag=args.tag,
        yaml_only=args.yaml_only,
        allow_destructive=args.allow_destructive,
        allow_contract_experiment = args.allow_contract_experiment,
        update_contract = args.update_contract,
    )

    if args.clean:
        clean_allure_results()
    print("执行命令")
    print(" ".join(str(item) for item in cmd))

    result = subprocess.run(cmd,cwd=ROOT_DIR)
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()