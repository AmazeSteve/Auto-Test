from pathlib import Path
from typing import Dict,Any,List,Union

import yaml

"""
处理yaml文件
"""
class YamlUtil:

    @staticmethod
    def load(file_path:Union[Path,str]) -> Any:
        """

        :param file_path: 需要解析的文件路径
        :return: 返回yaml解析数据
        """
        path = Path(file_path)

        if not  path.exists():
            raise FileNotFoundError(f"YAML文件不存在{path}")

        with open(path,'r',encoding='utf-8') as f:
            return yaml.safe_load(f)

    @staticmethod
    def load_all(file_path:Union[Path,str]) -> List[Any]:

        path = Path(file_path)
        if path.exists():
            raise FileNotFoundError(f"YAML文件不存在{path}")

        with open(path,'r',encoding='utf-8') as f:
            return list(yaml.safe_load_all(f))

    @staticmethod
    def read_as_dict(file_path:Union[Path,str]) -> Dict:

        data = YamlUtil.load(file_path)

        if not isinstance(data,Dict):
            raise TypeError(f"YAML内部不是dict:{file_path}")

        return data

    @staticmethod
    def read_as_list(file_path:Union[Path,str]) -> List:

        data = YamlUtil.load(file_path)

        if not isinstance(data, list):
            raise TypeError(f"YAML内部不是list:{file_path}")

        return data

if __name__ == '__main__':
    # data = YamlUtil.load(r'D:\Rxy-job\Project\Pyhon\Test-Data\wizbank_api_test\data\cases\user_group_cases.yaml')
    data = YamlUtil.load(r'/wizbank_api_test/data/cases/user/data/user_group_cases.yaml')
    print(data)