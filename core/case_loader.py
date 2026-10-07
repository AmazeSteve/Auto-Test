from pathlib import Path
from wizbank_api_test.utils.yaml_util import  YamlUtil
from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.core.case_normalizer import CaseNormalizer

"""
YAML 用例加载器。
负责：
1. 递归加载 data/cases 下所有 YAML
2. 跳过 enabled=False / enable=False 的用例
3. 注入 _case_file
4. 按 order 排序
"""
class CaseLoader:
    """
    YAML 用例加载器（纯静态方法）
    负责从文件系统加载用例数据，并进行初步过滤和排序
    """

    # 增加enabled enable 兼容吧
    @staticmethod
    def is_case_enabled(case: dict) -> bool:
        return case.get("enabled", case.get("enable", True)) is not False

    @classmethod
    def load_cases(cls,case_dir:Path,endpoint_dir: Path = None)->list:
        """
        加载所有 YAML 用例文件，返回已启用的用例列表

        执行步骤：
            1. 递归遍历 case_dir 下所有 .yaml 文件
            2. 使用 YamlUtil 读取每个文件（期望返回列表）
            3. 对每个用例检查 enabled 状态，跳过禁用的用例
            4. 注入 _case_file 字段，记录来源文件路径
            5. 按 order 升序排序，未设置 order 的默认 9999

        参数：
            case_dir: 用例数据根目录（Path 对象）

        返回：
            list[dict]: 已启用且排序后的用例列表
        """
        cases = []

        for yaml_file in case_dir.rglob("*.yaml"):
            file_cases = YamlUtil.read_as_list(yaml_file)

            for case in file_cases:
                # 增加用例enabled 判断 + enable 兼容 传入
                if not cls.is_case_enabled(case):
                    continue
                case["_case_file"] = str(yaml_file)
                cases.append(case)
        # 2. 如果有endpoint目录 ,进行规范化
        if endpoint_dir and endpoint_dir.exists():
            registry = EndpointRegistry(endpoint_dir)
            normalized_cases = []
            for case in cases:
                try:
                    normalized = CaseNormalizer.normalize_case(case, registry)
                    # 保留原始 _case_file（normalize_case 会复制，但可能丢失，确保保留）
                    normalized["_case_file"] = case.get("_case_file")
                    normalized_cases.append(normalized)
                except ValueError as e:
                    # 若 endpoint 不存在，抛出明确错误，终止加载
                    raise ValueError(f"用例 {case.get('id', 'UNKNOWN')} 引用不存在的 endpoint: {e}")
            cases = normalized_cases

        # 按 order 升序排序，未设置 order 的默认 9999
        cases.sort(key=lambda x: x.get("order", 9999))
        return cases