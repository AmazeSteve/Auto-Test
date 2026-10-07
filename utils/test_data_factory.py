"""运行期测试数据提供器。

职责边界：
1. 稳定测试夹具值，例如目标测试用户名，从环境变量读取。
2. 临时变更值，例如 destructive Case 的显示名称，按运行生成唯一值。
3. 不负责模板解析，也不读取/修改 Effect 状态。
"""
from datetime import  datetime
import os
import  re
import secrets

class TestDataFactory:
    """生成或提供 YAML 可引用的测试数据"""
    DEFAULT_TEST_USERNAME = "ee-Rey-15" #暂时硬编码
    DEFAULT_TEST_PREFIX = "api-test"

    @classmethod
    def supported_keys(cls)-> set[str]:
        return {
            "test_username",
            "unique_display_name"
        }

    @classmethod
    def resolve(cls, key: str):
        providers = {
            "test_username": cls.test_username,
            "unique_display_name": cls.unique_display_name,
        }
        provider = providers.get(key)
        if provider is None:
            supported = ", ".join(sorted(providers))
            raise ValueError(
                f"不支持的测试数据键: {key}，可用值: {supported}"
            )
        return provider()
    @classmethod
    def test_username(cls) -> str:
        """稳定测试用户标识，可通过 TEST_USERNAME 覆盖。"""
        value = os.getenv("TEST_USERNAME", cls.DEFAULT_TEST_USERNAME).strip()
        if not value:
            raise ValueError("TEST_USERNAME 不能为空")
        return value

    @classmethod
    def unique_display_name(cls) -> str:
        """生成本次 pytest 进程内用于状态变更验证的唯一显示名称。"""
        raw_prefix = os.getenv("TEST_PREFIX", cls.DEFAULT_TEST_PREFIX).strip()
        prefix = cls._sanitize_prefix(raw_prefix or cls.DEFAULT_TEST_PREFIX)

        timestamp = datetime.now().strftime("%m%d%H%M%S")
        suffix = secrets.token_hex(2)

        # 保持值短小，降低碰到业务字段长度限制的概率。
        return f"{prefix}-disp-{timestamp}-{suffix}"


    @staticmethod
    def _sanitize_prefix(value: str) -> str:
        value = re.sub(r"\s+", "-", value.strip())
        value = re.sub(r"[^0-9A-Za-z_.-]", "", value)
        value = value.strip("-._")
        return value[:24] or "api-test"