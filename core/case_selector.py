class CaseSelector:
    """
    用例选择器。
    负责：
    1. 根据 tag 选择用例
    2. 自动带上依赖用例
    3. 判断当前 case 是否应执行
    """

    # 新增依赖闭包函数
    @staticmethod
    def build_case_map(cases: list) -> dict:
        # 构建用例 ID -> 用例字典 的映射，方便快速查找
        return {
            case.get('id'): case for case in cases if case.get('id')
        }
    @staticmethod
    def normalize_depends(depends_on):
        """
        标准化 depends_on 字段为列表

        支持：
            - None / 空值 → []
            - 字符串 "WB_USER_001" → ["WB_USER_001"]
            - 列表 ["WB_USER_001", "WB_USER_002"] → 原样返回
        """
        if not depends_on:
            return []
        if isinstance(depends_on, str):
            return [depends_on]
        if isinstance(depends_on, list):
            return depends_on
        raise TypeError(f"depends_on 类型不支持:{type(depends_on)}")

    @classmethod
    def collect_dependency_ids(cls,case: dict, case_map: dict, collected=None) -> set:
        """
        递归收集某个用例的所有依赖 ID（包括间接依赖）

        参数：
            case: 当前用例字典
            case_map: ID -> case 的映射
            collected: 已收集的依赖 ID 集合（递归使用）

        返回：
            set: 所有依赖的用例 ID

        示例：
            若 A depends_on B，B depends_on C，则 collect(A) 返回 {B, C}
        """
        if collected is None:
            collected = set()
        depends_on = cls.normalize_depends(case.get("depends_on", []))

        for dep_id in depends_on:
            if dep_id in collected:
                continue
            collected.add(dep_id)
            # 根据当前用例的 depend 依赖那个用例ID 查找目标的用例-存在加入收集
            dep_case = case_map.get(dep_id)
            if dep_case:
                cls.collect_dependency_ids(dep_case, case_map, collected)

        return collected
    @classmethod
    def build_selected_case_ids_by_tag(cls,cases: list, target_tag: str) -> set:
        """
         根据 --tag 选择目标用例，并自动带上它们的所有前置依赖

         参数：
             cases: 所有用例列表
             target_tag: 目标标签（如 smoke）

         返回：
             set: 需要执行的用例 ID 集合（包含目标用例及其所有依赖）

         示例：
             若 WB_USER_GROUP_003 带有 dependency 标签且依赖 WB_USER_GROUP_001，
             则 --tag dependency 会选中 {WB_USER_GROUP_003, WB_USER_GROUP_001}
         """
        if not target_tag:
            # 未指定 --tag 时，返回所有用例 ID
            return {case.get("id") for case in cases if case.get("id")}
        case_map = cls.build_case_map(cases)
        selected_ids = set()
        for case in cases:
            # 遍历每一个用例的 tag 标记
            tags = case.get("tags", [])
            # 当前 命令行的 target_tag = tag 【smoke / dependency】等 看是否在这些用例中
            if target_tag in tags:
                case_id = case.get("id")
                selected_ids.add(case_id)
                # 收集该用例的所有依赖
                dependency_ids = cls.collect_dependency_ids(case, case_map)
                selected_ids.update(dependency_ids)
        return selected_ids

    @classmethod
    def should_run_by_tag(cls,case:dict,cases:list,target_tag:str)->bool:
        """
        判断当前用例是否应该被执行（基于 --tag 过滤）

        参数：
            case: 当前用例字典
            cases: 所有用例列表
            target_tag: 命令行指定的标签

        返回：
            bool: True 表示应该执行，False 表示应该跳过
        """
        if not target_tag:
            return True
        selected_ids = cls.build_selected_case_ids_by_tag(cases, target_tag)
        return case.get("id") in selected_ids