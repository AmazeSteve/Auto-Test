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
from copy import deepcopy
import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from wizbank_api_test.utils.yaml_util import YamlUtil


class EndpointRegistry:
    """接口注册表 (单例模式、全局唯一) """
    _instance = None

    def __new__(cls, endpoint_dir: Optional[Path] = None):
        """单例模式: 确保进程内复用同一实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, endpoint_dir: Optional[Path] = None):
        """
        初始化注册表
        参数
        :param endpoint_dir: endpoint YAML 文件根目录(Path 对象)
        若为 None，获取已经成功初始化的 active Registry。
        """
        try:
            self._initialize(endpoint_dir)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            # Pytest renders function arguments even without --showlocals.
            endpoint_dir = None
            raise error.with_traceback(None) from None

    def _initialize(self, endpoint_dir: Optional[Path]):
        if not hasattr(self, "_loaded"):
            self._endpoint_map: Dict[str, dict] = {}
            self._source_dir: Optional[Path] = None
            self._loaded = False

        if endpoint_dir is None:
            if not self._loaded:
                raise RuntimeError("Endpoint Registry has not been initialized")
            return

        source = self._canonical_source(endpoint_dir)
        if not self._loaded or source != self._source_dir:
            self.load(source)

    @property
    def source_dir(self) -> Optional[Path]:
        """Canonical source of the active valid snapshot (read-only)."""
        return self._source_dir

    @staticmethod
    def _canonical_source(endpoint_dir: Path) -> Path:
        try:
            return Path(endpoint_dir).resolve()
        except (TypeError, ValueError):
            raise TypeError("Endpoint source must be a filesystem path") from None
        except (OSError, RuntimeError):
            raise OSError("Cannot resolve endpoint source directory") from None

    def load(self, endpoint_dir: Path):
        """
        加载 endpoint 目录下所有的 yaml文件

        :param endpoint_dir:  endpoint YAML文件根目录
        异常:
            ValueError: 存在重复的 endpoint id
        :return:
        """
        try:
            self._load_source(endpoint_dir)
        except (OSError, TypeError, ValueError) as error:
            # Keep private source paths and parsed YAML out of startup tracebacks.
            endpoint_dir = None
            raise error.with_traceback(None) from None

    def _load_source(self, endpoint_dir: Path):
        source = self._canonical_source(endpoint_dir)
        try:
            if not source.exists():
                raise FileNotFoundError("Endpoint directory does not exist")
            if not source.is_dir():
                raise NotADirectoryError("Endpoint source is not a directory")
        except OSError as error:
            if isinstance(error, FileNotFoundError):
                raise FileNotFoundError("Endpoint directory does not exist") from None
            if isinstance(error, NotADirectoryError):
                raise NotADirectoryError("Endpoint source is not a directory") from None
            raise OSError("Cannot access endpoint source directory") from None

        endpoint_map = {}
        seen_ids = set()

        try:
            yaml_files = []
            directories = [source]
            while directories:
                with os.scandir(directories.pop()) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        if path.match("*.yaml"):
                            yaml_files.append(path)
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(path)
            yaml_files.sort(key=lambda path: path.relative_to(source).as_posix())
        except OSError:
            raise OSError("Cannot scan endpoint source directory") from None

        for yaml_file in yaml_files:
            try:
                file_data = YamlUtil.read_as_list(yaml_file)
            except (yaml.YAMLError, UnicodeError):
                raise ValueError("Invalid endpoint YAML") from None
            except TypeError:
                raise TypeError("Endpoint YAML must contain a list") from None
            except OSError:
                raise OSError("Cannot read endpoint YAML") from None
            for endpoint in file_data:
                if not isinstance(endpoint, dict):
                    raise TypeError("Endpoint definition must be a mapping")
                endpoint_id = endpoint.get("id")
                if not endpoint_id:
                    raise ValueError("Endpoint definition is missing an id")
                try:
                    hash(endpoint_id)
                except TypeError:
                    raise ValueError("Endpoint id must be hashable") from None
                if endpoint_id in seen_ids:
                    raise ValueError("Duplicate endpoint id")
                # 记录来源文件
                endpoint["_file"] = str(yaml_file)
                endpoint_map[endpoint_id] = endpoint
                seen_ids.add(endpoint_id)

        self._endpoint_map = endpoint_map
        self._source_dir = source
        self._loaded = True

    def get(self, endpoint_id: str) -> dict:
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
        return deepcopy(self._endpoint_map[endpoint_id])

    def exists(self, endpoint_id: str) -> bool:
        """检查 endpoint 是否存在"""
        return endpoint_id in self._endpoint_map

    def list_ids(self) -> List[str]:
        """列出所有 endpoint id"""
        return list(self._endpoint_map.keys())
