from pathlib import Path
from collections.abc import Mapping
from copy import deepcopy

import  yaml

"""
加载配置文件
"""
class Config:
    def __init__(self,env:str = 'local'):
        self.env = env
        self.root_dir = Path(__file__).resolve().parents[1]
        self.config_path = self.root_dir / 'configs' / f'env.{env}.yaml'
        self.data = self._load_yaml(self.config_path)


    @staticmethod
    def _load_yaml(path:Path)->dict:
        if not path.exists():
            raise FileNotFoundError(f'配置文件不存在:{path}')
        with open(path,'r',encoding='utf-8') as f:
            return yaml.safe_load(f)

    # 根据命令行输入的变量参数找到配置文件的环境IP
    def get_base_url(self,host_key:str)->str:
        base_urls = self.data.get('base_urls',{})
        value = base_urls.get(host_key)

        if not value:
            raise ValueError(f"base_urls中未配置:{host_key}")

        return value.rstrip('/')

    def get_user(self,user_type:str)->dict:
        users = self.data.get('users',{})
        user = users.get(user_type)

        if not user:
            raise ValueError(f"users中未配置用户:{user_type}")

        return user

    def get_credential_user(self, host_key: str, principal: str) -> dict:
        """Resolve exact host credentials; legacy users belong only to Host-be."""
        if (not isinstance(host_key, str) or not host_key
                or principal not in ("admin", "learner")):
            raise ValueError("Unsupported credential identity")

        credentials = self.data.get("credentials", {})
        if not isinstance(credentials, Mapping):
            raise ValueError("Host-bound credential configuration is invalid")
        if host_key in credentials:
            host_credentials = credentials[host_key]
            if not isinstance(host_credentials, Mapping):
                raise ValueError("Host-bound credential configuration is invalid")
            if principal in host_credentials:
                return self._credential_user(host_credentials[principal])

        if host_key != "Host-be":
            raise ValueError("Host-bound credential configuration is missing")
        users = self.data.get("users", {})
        if not isinstance(users, Mapping) or principal not in users:
            raise ValueError("Credential configuration is missing")
        return self._credential_user(users[principal])

    @staticmethod
    def _credential_user(user) -> dict:
        if (not isinstance(user, Mapping)
                or any(not isinstance(user.get(key), str) or not user[key].strip()
                       for key in ("username", "password"))):
            raise ValueError("Credential configuration is invalid")
        return deepcopy(dict(user))
    # 配置文件默认请求头内容 可用update更新
    def get_default_headers(self)->dict:
        return self.data.get('default_headers',{})
    # 配置文件默认超时时间
    def get_timeout(self)->int:
        return self.data.get('request',{}).get('timeout',30)

    # 配置文件是否要进行ssl验证,请求外网设置为False否则需要提供证书
    def get_verify_ssl(self)->bool:
        return self.data.get("request",{}).get("verify_ssl",False)
