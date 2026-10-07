"""v2.4.6A /upd_user Partial Update 契约专项入口。

该文件与主 data/cases 套件隔离，避免实验用例污染常规 destructive 回归。
真实写入需要同时满足：
1. --tag update_contract
2. --allow-destructive
3. --allow-contract-experiment
4. 显式环境变量 TEST_USERNAME
"""
from pathlib import Path

import pytest

from wizbank_api_test.core.case_loader import CaseLoader
from wizbank_api_test.core.case_selector import CaseSelector
from wizbank_api_test.core.case_validator import CaseValidator
from wizbank_api_test.core.lifecycle_runner import LifecycleRunner

ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = ROOT / "data" / "cases"
EXPERIMENT_DIR = ROOT / "data" / "experiments"
ENDPOINT_DIR = ROOT / "data" / "endpoints"

BASE_CASES = CaseLoader.load_cases(CASE_DIR, ENDPOINT_DIR)
EXPERIMENT_CASES = CaseLoader.load_cases(EXPERIMENT_DIR, ENDPOINT_DIR)
ALL_CASES = sorted(
    BASE_CASES + EXPERIMENT_CASES,
    key=lambda item: item.get("order", 9999),
)
CaseValidator.validate_all(ALL_CASES)

CASE_MAP = {case["id"]: case for case in ALL_CASES}
MANAGED_CHILDREN = LifecycleRunner.get_managed_child_ids(ALL_CASES)
SELECTED_IDS = CaseSelector.build_selected_case_ids_by_tag(
    ALL_CASES,
    "update_contract",
)

# 生命周期 child 在 root 内部执行，不作为独立 pytest item 再跑一次。
EXECUTION_CASES = [
    case
    for case in ALL_CASES
    if case.get("id") in SELECTED_IDS
    and case.get("id") not in MANAGED_CHILDREN
]
EXECUTION_IDS = [case["id"] for case in EXECUTION_CASES]


class TestUserUpdateContract:
    @pytest.mark.parametrize("case_id", EXECUTION_IDS, ids=EXECUTION_IDS)
    def test_update_contract(
        self,
        api_client_factory,
        admin_cookie,
        request,
        case_id,
    ):
        target_tag = request.config.getoption("--tag")
        if target_tag != "update_contract":
            pytest.skip("契约专项仅允许通过 --tag update_contract 显式执行")

        case = CASE_MAP[case_id]
        if case.get("lifecycle"):
            LifecycleRunner.run_lifecycle(
                root_case=case,
                case_map=CASE_MAP,
                api_client_factory=api_client_factory,
                admin_cookie=admin_cookie,
                request=request,
            )
            return

        LifecycleRunner.run_case(
            case=case,
            api_client_factory=api_client_factory,
            admin_cookie=admin_cookie,
            request=request,
        )
