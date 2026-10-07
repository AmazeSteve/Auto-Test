from jsonpath_ng import parse
from wizbank_api_test.core.response_parser import ResponseParser

"""
支持的断言类型：
    - status_code       : 校验 HTTP 状态码
    - contains          : 响应文本是否包含指定字符串
    - not_contains      : 响应文本是否不包含指定字符串
    - json_path_exist   : JSONPath 表达式是否存在
    - json_path_equals  : JSONPath 表达式的值是否等于预期
    - json_path_type    : JSONPath 表达式的值是否属于指定类型
    - json_path_not_empty :验证 JSONPath 值不为 None/""/[]/{}
    - pagination        : 	通用分页断言（支持自定义 JSONPath）
    - list_field_contains : 检查列表中指定字段是否包含期望值（any/all 模式）
"""
class Assertor:
    """
    1. 测试类不直接写大量 response 判断
    2. YAML validate 可以统一映射到这里
    3. 后续 HTML/XML/302 文件断言都继续扩展这里。

    """
    @staticmethod
    def assert_status_code(response,expected:int):
        actual = response.status_code

        assert actual == expected,(f"状态码断言失败,expected={expected},actual={actual},"
                                   f"response={response.text[:500]}")

    @staticmethod
    def assert_contains(response,expected:str):
        text = response.text
        assert  expected in text,f"响应内容不包含:{expected},response={text[:500]}"


    @staticmethod
    def assert_not_contains(response,expected:str):
        text = response.text
        assert expected not in text, f"响应内容不应包含:{expected},response={text[:500]}"

    @staticmethod
    def assert_json_path_exists(response,expression:str):
         # json_data = response.json()
        # 使用响应解析器
        json_data = ResponseParser.parse_json(response)
        matches = parse(expression).find(json_data) # 解析定位表达式

        assert  matches,f"JsonPath 不存在:{expression},response={json_data}"

    @staticmethod
    def assert_json_path_equals(response, expression: str, expected):
        """
        断言 JSON 响应中指定 JSONPath 的值等于预期值

        参数：
            response: requests.Response 对象
            expression: JSONPath 表达式
            expected: 期望的值（任意类型，与 JSON 值比较）

        注意：
            - 若表达式匹配多个值，只取第一个（matches[0]）
            - 若需要匹配多个，应使用 json_path_type 或自定义扩展
        """
        # json_data = response.json()
        # 使用响应解析器
        json_data = ResponseParser.parse_json(response)
        matches = parse(expression).find(json_data)

        # 先确保路径存在
        assert matches, f"JSONPath 不存在: {expression}, response={json_data}"

        actual = matches[0].value
        assert actual == expected, (
            f"JSONPath 断言失败：{expression}, expected={expected}, actual={actual}"
        )

    @staticmethod
    def assert_json_path_type(response, expression: str, expected_type: str):
        """
        断言 JSON 响应中指定 JSONPath 的值的类型是否符合预期

        参数：
            response: requests.Response 对象
            expression: JSONPath 表达式
            expected_type: 期望的类型名称，支持：
                "dict", "list", "str", "int", "float", "bool", "none"

        异常：
            - 若表达式无匹配，抛出 AssertionError
            - 若 expected_type 不在支持列表中，抛出 ValueError
            - 若类型不匹配，抛出 AssertionError
        """
        # json_data = response.json()
        # 使用响应解析器
        json_data = ResponseParser.parse_json(response)
        matches = parse(expression).find(json_data)

        assert matches, f"JSONPath 不存在: {expression}, response={json_data}"

        actual_value = matches[0].value

        # 类型名称到 Python 类型的映射
        type_mapping = {
            "dict": dict,
            "list": list,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "none": type(None)
        }

        if expected_type not in type_mapping:
            raise ValueError(f"不支持断言类型: {expected_type}")

        expected_py_type = type_mapping[expected_type]

        if expected_type == "int":
            valid = (
                    isinstance(actual_value, int)
                    and not isinstance(actual_value, bool)
            )
        else:
            valid = isinstance(
                actual_value,
                expected_py_type
            )

        assert valid, (
            f"JSONPath 类型断言失败: "
            f"{expression}, "
            f"actual_type={type(actual_value).__name__}, "
            f"expected_type={expected_type}"
        )

    @staticmethod
    def assert_json_path_not_empty(response, expression: str):
        # json_data = response.json()
        # 使用响应解析器
        json_data = ResponseParser.parse_json(response)
        matches = parse(expression).find(json_data)

        assert matches, (
            f"JSONPath 不存在: {expression}"
        )

        actual = matches[0].value

        assert actual not in (None, "", [], {}), (
            f"JSONPath 内容为空: {expression}, "
            f"actual={actual}"
        )

    """增加 JSONPath 取值辅助方法"""
    @staticmethod
    def _get_json_path_value(json_data, expression: str):
        """
        从已经解析好的 JSON 数据中获取指定 JSONPath 的第一个值。

        用于复杂断言内部复用，避免每个字段重复编写：
            parse()
            find()
            matches 判断
        """
        matches = parse(expression).find(json_data)

        assert matches, (
            f"JSONPath 不存在: {expression}, "
            f"response={json_data}"
        )

        return matches[0].value

    @classmethod
    def assert_pagination(cls, response, rule: dict):
        """
        通用分页响应断言。

        默认支持如下结构：

            {
                "total": 42,
                "totalPage": 5,
                "pageSize": 10,
                "page": 1,
                "rows": [...]
            }

        JSONPath 可以由 YAML 自定义，因此也能适配：

            $.data.total
            $.data.list
            $.records
            等不同接口分页格式。
        """

        json_data = ResponseParser.parse_json(response)

        """
        第一，我没有用 isinstance(total, int) 分页里的 total=True 显然不应该被认为是合法整数，所以额外排除了 bool。
        第二，没有强制 rows 非空
        row 可能出现 row = [] total = 0
        """
        total_path = rule.get("total", "$.total")
        total_page_path = rule.get("total_page", "$.totalPage")
        page_path = rule.get("page", "$.page")
        page_size_path = rule.get("page_size", "$.pageSize")
        rows_path = rule.get("rows", "$.rows")

        total = cls._get_json_path_value(
            json_data,
            total_path
        )

        total_page = cls._get_json_path_value(
            json_data,
            total_page_path
        )

        page = cls._get_json_path_value(
            json_data,
            page_path
        )

        page_size = cls._get_json_path_value(
            json_data,
            page_size_path
        )

        rows = cls._get_json_path_value(
            json_data,
            rows_path
        )

        # ---------- 基础类型 ----------

        assert isinstance(total, int) and not isinstance(total, bool), (
            f"pagination.total 类型错误: "
            f"actual={total}, type={type(total).__name__}"
        )

        assert isinstance(total_page, int) and not isinstance(total_page, bool), (
            f"pagination.total_page 类型错误: "
            f"actual={total_page}, type={type(total_page).__name__}"
        )

        assert isinstance(page, int) and not isinstance(page, bool), (
            f"pagination.page 类型错误: "
            f"actual={page}, type={type(page).__name__}"
        )

        assert isinstance(page_size, int) and not isinstance(page_size, bool), (
            f"pagination.page_size 类型错误: "
            f"actual={page_size}, type={type(page_size).__name__}"
        )

        assert isinstance(rows, list), (
            f"pagination.rows 必须为 list, "
            f"actual_type={type(rows).__name__}"
        )

        # ---------- 基础关系 ----------

        assert total >= 0, (
            f"pagination.total 不应小于 0: {total}"
        )

        assert page >= 1, (
            f"pagination.page 应 >= 1: {page}"
        )

        assert page_size > 0, (
            f"pagination.page_size 应 > 0: {page_size}"
        )

        assert len(rows) <= page_size, (
            f"pagination.rows 数量超过 pageSize: "
            f"rows={len(rows)}, pageSize={page_size}"
        )

        # ---------- 当前页最大可返回数量 ----------
        """
        total = 42
        pageSize = 10
        
        page 1 → 最多 10
        page 2 → 最多 10
        page 3 → 最多 10
        page 4 → 最多 10
        page 5 → 最多 2
        """
        # if total > 0:
        #     start_index = (page - 1) * page_size

        #     if start_index < total:
        #         expected_max_rows = min(
        #             page_size,
        #             total - start_index
        #         )

        #         assert len(rows) <= expected_max_rows, (
        #             f"pagination 当前页 rows 数量异常: "
        #             f"page={page}, "
        #             f"pageSize={page_size}, "
        #             f"total={total}, "
        #             f"expected_max_rows={expected_max_rows}, "
        #             f"actual_rows={len(rows)}"
        #         )

        # 调整为严格分页 + 宽松分页政策
        # ---------- 当前页理论记录数 ----------
        start_index = (page - 1) * page_size
        if total == 0 or start_index >=total :
            expected_rows = 0 
        else:
            expected_rows = min(
                page_size,
                total - start_index
                )
        strict_rows = rule.get("strict_rows",False) 
        if strict_rows:
            # 标准分页
                 assert len(rows) == expected_rows, (
                    f"pagination 当前页 rows 数量异常: "
                    f"page={page}, "
                    f"pageSize={page_size}, "
                    f"total={total}, "
                    f"expected_rows={expected_rows}, "
                    f"actual_rows={len(rows)}"
            )
        else:
            # 宽松分页
            # 不允许超过理论最大数量
                    assert len(rows) <= expected_rows, (
                    f"pagination 当前页 rows 数量异常: "
                    f"page={page}, "
                    f"pageSize={page_size}, "
                    f"total={total}, "
                    f"expected_max_rows={expected_rows}, "
                    f"actual_rows={len(rows)}"
                )
        assert total >= len(rows), (
            f"pagination.total 小于当前 rows 数量: "
            f"total={total}, rows={len(rows)}"
        )

        # ---------- 请求值回显 ----------

        expected_page = rule.get("expected_page")
        if expected_page is not None:
            assert page == expected_page, (
                f"pagination.page 不符合预期: "
                f"expected={expected_page}, actual={page}"
            )

        expected_page_size = rule.get("expected_page_size")
        if expected_page_size is not None:
            assert page_size == expected_page_size, (
                f"pagination.pageSize 不符合预期: "
                f"expected={expected_page_size}, actual={page_size}"
            )

        # ---------- totalPage 数学关系 ----------

        check_total_page = rule.get(
            "check_total_page",
            True
        )

        if check_total_page:
            if total == 0:
                expected_total_page = 0
            else:
                expected_total_page = (
                                              total + page_size - 1
                                      ) // page_size

            # 一些系统 total=0 时返回 totalPage=1，
            # 因此空数据时暂时兼容 0 和 1。
            if total == 0:
                assert total_page in (0, 1), (
                    f"pagination.totalPage 异常: "
                    f"total=0, totalPage={total_page}"
                )
            else:
                assert total_page == expected_total_page, (
                    f"pagination.totalPage 计算错误: "
                    f"expected={expected_total_page}, "
                    f"actual={total_page}, "
                    f"total={total}, "
                    f"pageSize={page_size}"
                )
    # 一个列表里指定字段是否包含某个值
    @classmethod
    def assert_list_field_contains(
        cls,
        response,
        source: str,
        field: str,
        expected,
        match: str = "any",
        case_sensitive: bool = True,
    ):
        """
        检查 JSONPath 指向的列表中，
        指定字段是否包含 expected。

        match:
            any -> 至少一条匹配
            all -> 所有记录都匹配
        """
        json_data = ResponseParser.parse_json(response)
        data_list = cls._get_json_path_value(
            json_data, source
        )
        assert isinstance(data_list, list), (
            f"list_field_contains source 必须为 list: "
            f"source={source}, "
            f"actual_type={type(data_list).__name__}"
        )
        assert data_list,(
            f"list_field_contains 列表为空: "
            f"source={source}"
        )
        expected_text = str(expected)
        if not case_sensitive:
            expected_text = expected_text.lower()

        results = []

        for index, item in enumerate(data_list):

            assert isinstance(item, dict), (
                f"list_field_contains 列表元素不是 dict: "
                f"index={index}, "
                f"actual_type={type(item).__name__}"
            )

            assert field in item, (
                f"list_field_contains 字段不存在: "
                f"index={index}, "
                f"field={field}, "
                f"item={item}"
            )

            actual = item[field]

            actual_text = (
                ""
                if actual is None
                else str(actual)
            )

            if not case_sensitive:
                actual_text = actual_text.lower()

            results.append(
                expected_text in actual_text
            )

        if match == "any":
            assert any(results), (
                f"列表中没有记录满足字段包含条件: "
                f"source={source}, "
                f"field={field}, "
                f"expected={expected}"
            )

        elif match == "all":
            assert all(results), (
                f"列表中存在记录不满足字段包含条件: "
                f"source={source}, "
                f"field={field}, "
                f"expected={expected}"
            )

        else:
            raise ValueError(
                f"list_field_contains 不支持 match={match}, "
                f"仅支持 any/all"
            )
    """运行的校验类"""
    @classmethod
    def run_validations(cls,response,validations:list):
        """

        批量执行断言规则（核心方法）

        参数：
            response: requests.Response 对象
            validations: 断言规则列表，每个规则为一个字典，包含：
                - "type"       : 断言类型（必填），对应上述静态方法名
                - 其他字段      : 根据不同 type 提供，如 "expected", "expression"

        支持的规则格式示例：
            {"type": "status_code", "expected": 200}
            {"type": "contains", "expected": "success"}
            {"type": "json_path_equals", "expression": "$.code", "expected": 0}
            {"type": "json_path_type", "expression": "$.data", "expected": "dict"}

        异常：
            - 遇到不支持的断言类型，抛出 ValueError
            - 具体断言失败由相应方法抛出 AssertionError
        :param response:
        :param validations:
        :return:
        """
        if not validations:
            return

        for item in validations:
            assert_type = item.get("type")

            if assert_type == "status_code":
                cls.assert_status_code(response,item["expected"])

            elif assert_type == "contains":
                cls.assert_contains(response,item["expected"])
            elif assert_type == "not_contains":
                cls.assert_not_contains(response,item["expected"])

            elif assert_type == "json_path_exists":
                # 定位表达式
                cls.assert_json_path_exists(response,item["expression"])
            elif assert_type == "json_path_equals":
                cls.assert_json_path_equals(
                    response,item["expression"],
                    item["expected"]
                )

            elif assert_type == "json_path_type":
                expected_type = item.get(
                    "expected_type",
                    item.get("expected")
                )

                if not expected_type:
                    raise ValueError(
                        "json_path_type 缺少 expected_type"
                    )

                cls.assert_json_path_type(
                    response,
                    item["expression"],
                    expected_type
                )
            elif assert_type == "json_path_not_empty":
                cls.assert_json_path_not_empty(
                    response,
                    item["expression"]
                )
            elif assert_type == "pagination":
                cls.assert_pagination(
                    response,
                    item
                )
            elif assert_type == "list_field_contains":
                cls.assert_list_field_contains(
                    response=response,
                    source=item["source"],
                    field=item["field"],
                    expected=item["expected"],
                    match=item.get("match", "any"),
                    case_sensitive=item.get(
                        "case_sensitive",
                        True
                    ),
                )
            else:
                raise ValueError(f"不支持断言类型{assert_type}")
