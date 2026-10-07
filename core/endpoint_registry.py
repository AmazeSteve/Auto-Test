"""
接口注册表（Endpoint Registry）

职责：
    1. 加载 data/endpoints/**/*.yaml 中的接口定义
    2. 校验 endpoint id 唯一性
    3. 提供根据 endpoint id 获取接口定义的能力

设计原则：
    - endpoint 定义是接口的“模板”，包含 method、path、headers、auth、默认 tags 等
    - endpoint 不包含业务数据（如具体参数值、断言、提取规则）
    - endpoint 可以被多个 case 复用，减少重复配置

使用方式：
    registry = EndpointRegistry(ENDPOINT_DIR)
    endpoint = registry.get("EP_USER_GET_FORM")
"""
from pathlib import  Path
from typing import Dict,List,Optional
from wizbank_api_test.utils.yaml_util import YamlUtil

class EndpointRegistry:
    """接口注册表 (单例模式、全局唯一) """
    _instance = None

    def __new__(cls, endpoint_dir:Optional[Path] = None):
        """单例模式: 确保全局只加载一次"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, endpoint_dir:Optional[Path] = None):
        """
        初始化注册表
        参数
        :param endpoint_dir: endpoint YAML 文件根目录(Path 对象)
        若为 None ,则不自动加载，需手动调用load()
        """
        if hasattr(self,"_loaded") and self._loaded:
            return # 已加载，不重复初始化

        self._endpoint_map :Dict[str,dict] = {}
        self._loaded = False

        if endpoint_dir is not None:
            self.load(endpoint_dir)

    def load(self, endpoint_dir:Path):
        """
        加载 endpoint 目录下所有的 yaml文件

        :param endpoint_dir:  endpoint YAML文件根目录
        异常:
            ValueError: 存在重复的 endpoint id
        :return:
        """
        if not endpoint_dir.exists():
            raise FileNotFoundError(f"Endpoint 目录不存在 {endpoint_dir} ")

        endpoint_map = {}
        seen_ids = set()

        for yaml_file in endpoint_dir.rglob("*.yaml"):
            file_data = YamlUtil.read_as_list(yaml_file)
            for endpoint in file_data:
                endpoint_id = endpoint.get("id")
                if not endpoint_id:
                    raise ValueError(f"Endpoint 缺少 id 字段,文件: {yaml_file}")
                if endpoint_id in seen_ids:
                    raise ValueError(
                        f"重复的 endpoint_id:{endpoint_id},"
                        f"已在文件 {endpoint_map[endpoint_id]["_file"]}"
                    )
                # 记录来源文件
                endpoint["_file"] = str(yaml_file)
                endpoint_map[endpoint_id] = endpoint
                seen_ids.add(endpoint_id)

        self._endpoint_map = endpoint_map
        self._loaded = True
    def get(self, endpoint_id:str) -> dict:
        """
        根据 endpoint id 获取接口定义

        参数：
            endpoint_id: 接口标识

        返回：
            dict: 接口定义（包含 method、path、headers、auth 等）

        异常：
            ValueError: 若 endpoint_id 不存在
        """
        if endpoint_id not in self._endpoint_map:
            raise ValueError(f"Endpoint 不存在: {endpoint_id}")

        # 返回副本， 避免外部修改影响原数据
        return self._endpoint_map[endpoint_id].copy()
    def exists(self, endpoint_id: str) -> bool:
        """检查 endpoint 是否存在"""
        return endpoint_id in self._endpoint_map

    def list_ids(self) -> List[str]:
        """列出所有 endpoint id"""
        return list(self._endpoint_map.keys())