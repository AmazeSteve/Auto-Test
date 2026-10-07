"""
本文件负责从响应里面提取变量，并写日 Context
"""
import re

"""
v0.4 新增支持：
    - regex        : 从响应文本中通过正则表达式提取
    - find_in_list : 从 JSONPath 定位的列表中按条件查找目标对象并提取字段
"""

from jsonpath_ng import parse
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.response_parser import ResponseParser

class Extractor:

    """
    响应变量提取层

    支持：
        1. header      : 从响应头中获取指定字段（如 Set-Cookie）
        2. json_path   : 从 JSON 响应体中通过 JSONPath 提取值
        3. cookie      : 从响应 Cookies 中获取（本类未直接封装，可扩展）
        4. regex       : 从响应文本中按正则表达式提取（新增）
        5. find_in_list: 从 JSONPath 定位到的列表中，按条件查找对象并提取字段（新增

    支持的提取类型：
    - header      : 从响应头中获取指定字段（如 Set-Cookie）
    - json_path   : 从 JSON 响应体中通过 JSONPath 提取值
    - cookie      : 从响应 Cookies 中获取指定名称的值（未直接封装，可扩展）
    - 从复杂 JSON 列表中按条件筛选特定对象（如根据 code 查找对应 ID）

使用场景：
    - 登录接口提取 Session/Cookie 存入 Context
    - 查询接口提取返回的列表、ID 等信息，供后续删除/修改接口使用
    """

    @staticmethod
    def extract_header(response,name:str):
        value = response.headers.get(name)
        if value is None:
            raise ValueError(f"Header 提取失败 不存在:{name}")
        return value

    @staticmethod
    def extract_json_path(response,expression:str):
        """
        从 JSON 响应中通过 JSONPath 提取值

        参数：
            response: requests.Response 对象（需包含 JSON 响应）
            expression: JSONPath 表达式，如 "$.data.userList"

        返回：
            - 若匹配单个值，返回该值（任意类型）
            - 若匹配多个值，返回列表（包含所有匹配值）

        异常：
            - 若 JSON 解析失败，由 response.json() 抛出
            - 若表达式无匹配，抛出 ValueError
        """
        # json_data = response.json()
        # 解析响应JSON
        json_data = ResponseParser.parse_json(response)
        matches = parse(expression).find(json_data) # 提取jsonpath 指定内容

        if not matches:
            raise ValueError(f"JSONPath 提取失败，不存在: {expression}")

        # 若只有一个匹配，直接返回该值（避免外层套列表）
        if len(matches) == 1:
            return matches[0].value

        # 多个匹配，返回所有值的列表
        return [match.value for match in matches]
    # regex
    @staticmethod
    def extract_regex(response,expression:str,group:int = 1):
        """
               从响应文本中通过正则表达式提取匹配组内容（新增）

               参数：
                   response: requests.Response 对象
                   expression: 正则表达式字符串（如 r'"token":"([^"]+)"'）
                   group: 匹配组索引（默认1，即第一个括号组）

               返回：
                   匹配到的组内容（字符串）

               异常：
                   若正则无匹配，抛出 ValueError
               """
        text = response.text or ""
        match = re.search(expression,text)
        if not match:
            raise ValueError(f"正则提取失败，不匹配: {expression}")
        return match.group(group)

    @staticmethod
    def extract_find_in_list(response,source:str,where:dict,get:str):
        """
       从 JSONPath 定位到的列表中，按字段查找目标对象，然后提取指定字段（新增）
        示例:
        source: $.data.userGroupList
        where:
            field:usg_code
            equals: TV Rey02用户组
        get: usg_ent_Id

        使用场景：
            当接口返回的列表包含多个对象，需要根据某个字段值找到特定对象并取出其另一个字段。
            例如：从用户组列表中根据 usg_code 匹配到目标组，取出其 usg_ent_id。

        参数：
            response: requests.Response 对象
            source: JSONPath 表达式，指向一个列表（如 "$.data.userGroupList"）
            where: 查找条件，格式 {"field": "字段名", "equals": "期望值"}
            get: 目标字段名（如 "usg_ent_id"）

        返回：
            目标对象中 get 字段的值

        异常：
            - 若 source 定位不到列表，抛出 ValueError
            - 若 source 结果不是 list，抛出 TypeError
            - 若列表中没有满足 where 条件的对象，抛出 ValueError
            - 若目标对象中不存在 get 字段，抛出 ValueError

        示例 YAML 用法：
            extract:
              target_id:
                type: find_in_list
                source: $.data.userGroupList
                where:
                  field: usg_code
                  equals: TV Rey02用户组
                get: usg_ent_id

        """
        # json_data = response.json()
        # 解析响应JSON
        json_data = ResponseParser.parse_json(response)
        matches = parse(source).find(json_data)
        if not matches:
            raise ValueError(f"find_in_list source 不匹配: {source}")
        data_list = matches[0].value

        if not isinstance(data_list,list):
            raise TypeError(f"find_in_list source结果不是list:{source}")
        field = where["field"]
        expected = where["equals"]

        for item in data_list:
            if isinstance(item,dict) and item.get(field) == expected:
                if get not in item:
                    raise ValueError(f"目标对象中不存在字段{get}")
                return item[get]
        raise ValueError(f"未在列表中找到{field} == {expected} 的对象")
    @classmethod
    def run_extractors(cls,response,extract_rules:dict):
        """

        extract:
          user_grp_list:
            type: json_path
            expression: $.data.userGroupList
        :param response:
        :param extract_rules:
        :return:
        """
        """
        批量执行提取规则，并将结果存入 Context
        
        参数：
            response: requests.Response 对象
            extract_rules: 提取规则字典，格式：
                {
                    "变量名1": {
                        "type": "header",
                        "name": "Set-Cookie"
                    },
                    "变量名2": {
                        "type": "json_path",
                        "expression": "$.data.token"
                    }
                    "变量名3": {
                        "type": "regex",
                        "expression": r'"token":"([^"]+)"',
                        "group": 1          # 可选，默认1
                    },
                    "变量名4": {
                        "type": "find_in_list",
                        "source": "$.data.userGroupList",
                        "where": {"field": "usg_code", "equals": "TV Rey02用户组"},
                        "get": "usg_ent_id"
                    }
                }
        
        返回：
            extracted: 提取结果的字典（变量名 -> 值），同时已写入 Context
        
        异常：
            - 若提取类型不支持，抛出 ValueError
            - 若提取过程失败，由具体方法抛出异常
        """

        if not extract_rules:
            return {}

        extracted  = {}

        for var_name,rule in extract_rules.items():
            extract_type = rule.get("type")

            if extract_type == "header":
                value = cls.extract_header(response,rule["name"])

            elif extract_type == "json_path":
                value = cls.extract_json_path(response,rule["expression"])

            elif extract_type == "regex":
                value = cls.extract_regex(
                    response,
                    rule["expression"],
                    rule.get("group",1)
                )
            elif extract_type == "find_in_list":
                value = cls.extract_find_in_list(
                    response,
                    source=rule["source"],
                    where=rule["where"],
                    get=rule["get"]
                )

            else:
                raise  ValueError(f"不支持的提取类型:{extract_type}")
            # 存入上下文变量中
            # Context.set(var_name,value)
            Context.set_variable(var_name, value)
            extracted[var_name] = value

        return extracted