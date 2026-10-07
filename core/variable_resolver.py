"""运行时变量解析器。
1. {{admin_Cookie}} 这种运行时变量
2. $timestamp_ms 当前毫秒时间戳
3. base64 密码编码
更新支持
支持:
1. {{key}}                    从 Context 读取运行时变量。
2. $timestamp_ms              当前毫秒时间戳。
3. {{$data.xxx}}              委托 TestDataFactory 提供测试数据。
"""

"""

是一个变量解析器，用于将字符串、字典或列表中的变量占位符替换为实际值。它主要服务于接口自动化测试中的动态参数化场景，
使得测试数据（如请求体、URL、Headers）能够引用运行时的变量（如登录后的 token、随机数据等），而无需硬编码。
"""

"""
解析运行中需要的变量
"""

"""
功能：
1. 替换字符串中的运行时变量占位符，如 {{admin_Cookie}}
2. 支持内置函数：$timestamp_ms（当前毫秒时间戳）
3. 递归处理字典、列表等复杂数据结构
4. 与 Context 全局存储配合，实现动态参数化

使用场景：
- 从 YAML/JSON 数据文件中读取模板，替换为实际运行值后发起请求
- 测试数据中引用登录后的 token、用户ID 等动态值
"""
import  re
import  time

from wizbank_api_test.core.context import Context
from wizbank_api_test.utils.test_data_factory import TestDataFactory

class VariableResolver:
    """

    变量解析器类（无状态，全部为类方法）
    解析字符串、字典、列表中的运行时占位符
    核心方法：resolve() 递归解析任意结构中的占位符
    支持格式：
        - {{key}}           → 从 Context 中获取 key 对应的值
        - $timestamp_ms     → 当前毫秒级时间戳字符串
    扩展：可添加更多内置函数（如 $random_uuid）通过新增静态方法实现

    """
    # 预编译正则表达式，匹配 {{任意内容}} 的占位符
    # 使用非贪婪匹配 (.+?) 确保匹配到第一个 }} 即停止
    VAR_PATTERN =  re.compile(r"\{\{(.+?)\}\}")
    DATA_PREFIX = "$data."
    DATA_CONTEXT_PREFIX = "data."
    _MISSING = object()
    @staticmethod
    def timestamp_ms()->str:
        """
               内置函数：返回当前时间的毫秒级时间戳（字符串形式）
               例如：1689123456789
        """
        return  str(int(time.time()*1000))
    # 已经在工具类新增 base64编码工具 - 此废弃
    # @staticmethod
    # def base64_encode(value:str)->str:
    #     return base64.b64encode(value.encode('utf-8')).decode('utf-8')
    @classmethod
    def _resolve_data_reference(cls,reference:str):
        key = reference[len(cls.DATA_PREFIX):].strip()
        if not key :
            raise ValueError("$data 引用缺少数据键")
        context_key = f"{cls.DATA_CONTEXT_PREFIX}{key}"
        cached = Context.get_variable(context_key, cls._MISSING)
        if cached is not cls._MISSING:
            return cached

        value = TestDataFactory.resolve(key)
        Context.set_variable(context_key, value)
        return value

    @classmethod
    def _resolve_placeholder(cls, var_name: str):
        var_name = var_name.strip()

        if var_name.startswith(cls.DATA_PREFIX):
            return cls._resolve_data_reference(var_name)

        # var_value = Context.get(var_name, cls._MISSING)
        var_value = Context.get_variable(var_name, cls._MISSING)
        if var_value is cls._MISSING:
            raise ValueError(f"变量未找到: {var_name}")

        return var_value
    """
    resolve 方法能够递归处理：

    字典（dict）：遍历所有键值对，对每个值进行解析。
    
    列表（list）：遍历每个元素，逐个解析。
    
    字符串（str）：检测是否包含变量占位符，并进行替换。

    这种设计使得测试数据（如 JSON 请求体）可以整体传入，无需人工逐层提取变量
    """
    """
           递归解析变量占位符的主方法

           处理逻辑：
               1. 如果是字典 → 遍历所有键值对，递归解析每个值
               2. 如果是列表 → 遍历所有元素，递归解析每个元素
               3. 如果不是字符串 → 直接返回原始值（数字、布尔、None 等）
               4. 如果是字符串：
                   a. 若值为 "$timestamp_ms" → 替换为当前毫秒时间戳
                   b. 若包含 {{...}} 占位符 → 使用正则替换，每个占位符从 Context 获取值
                   c. 若不包含任何占位符 → 原样返回

           参数：
               value: 任意类型（dict, list, str, int, float, bool, None）

           返回：
               解析后的新值（类型与输入保持一致）

           异常：
               当当占位符中的变量名在 Context 中不存在时，抛出 ValueError
    """
    @classmethod
    def resolve(cls,value):
        if isinstance(value,dict):
            return {k:cls.resolve(v) for k,v in value.items()}

        if isinstance(value,list):
            return [cls.resolve(v) for v in value]

        if isinstance(value, tuple):
            return tuple(cls.resolve(item) for item in value)

        if not isinstance(value, str):
            return value

        if value == "$timestamp_ms":
            return cls.timestamp_ms()

        # 允许直接写 $data.test_username，也兼容 YAML 中统一使用的
        # {{$data.test_username}} 形式。
        if value.startswith(cls.DATA_PREFIX) and "{{" not in value:
            return cls._resolve_data_reference(value)
        # 保持现有框架语义：{{key}} 替换结果仍以字符串进入请求模板。
        # 当前 $data 提供器也返回字符串，因此修改 Case 与验证 Case 行为一致。
        full_match = cls.VAR_PATTERN.fullmatch(value)
        if full_match:
            return str(cls._resolve_placeholder(full_match.group(1)))
        # 定义替换函数，供正则 sub 调用
        def replace_var(match):
            # match.group(1) 取出 {{ 和 }} 之间的变量名
            # var_name = match.group(1)
            # 从全局 Context 中获取变量值
            # var_value = Context.get(var_name)

            # 若变量不存在，抛出明确异常，便于调试
            # if var_value is None:
            #     raise ValueError(f'变量未找到: {var_name}')

            # 统一转为字符串返回（因为占位符在字符串中，替换后也应为字符串）
            # return str(var_value)

            resolved = cls._resolve_placeholder(match.group(1))
            return str(resolved)

        # 使用正则的 sub 方法，将所有 {{...}} 替换为对应的真实值
        # 如果字符串中没有匹配项，sub 会原样返回
        return cls.VAR_PATTERN.sub(replace_var, value)