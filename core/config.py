from pathlib import Path
from collections.abc import Mapping
from copy import deepcopy
from urllib.parse import urlsplit

import  yaml

"""
加载配置文件
"""
class Config:
    def __init__(self,env:str = 'local'):
        try:
            self._validate_env(env)
        except ValueError:
            # Traceback renderers may display argument values, including private paths.
            env = None
            raise ValueError("Configuration environment identifier is invalid") from None
        self.env = env
        self.root_dir = Path(__file__).resolve().parents[1]
        self.config_path = self.root_dir / 'configs' / f'env.{env}.yaml'
        try:
            self.data = self._load_yaml(self.config_path)
        except FileNotFoundError:
            raise FileNotFoundError("Configuration file is missing") from None
        except OSError:
            raise OSError("Configuration file could not be read") from None
        except UnicodeError:
            raise ValueError("Configuration file must be valid UTF-8") from None
        except yaml.YAMLError:
            raise ValueError("Configuration YAML is invalid") from None
        try:
            self._validate_data(self.data)
        except ValueError as error:
            # Keep raw configuration and private file paths out of traceback frames.
            raise ValueError(str(error)) from None

    @staticmethod
    def _load_yaml(path:Path)->dict:
        with open(path, 'r', encoding='utf-8') as stream:
            return yaml.safe_load(stream)

    @staticmethod
    def _validate_env(env):
        if (not isinstance(env, str) or not env or env == "." or ".." in env
                or not all(character.isalnum() or character in "_-." for character in env)):
            raise ValueError("Configuration environment identifier is invalid")

    @staticmethod
    def _validate_data(data):
        if not isinstance(data, Mapping):
            raise ValueError("Configuration root must be a Mapping")

        base_urls = data.get("base_urls", {})
        if not isinstance(base_urls, Mapping):
            raise ValueError("Configuration base_urls must be a Mapping")
        for host_key, url in base_urls.items():
            if not isinstance(host_key, str) or not host_key.strip():
                raise ValueError("Configuration base_urls host key must be a non-empty string")
            if not isinstance(url, str) or not url.strip():
                raise ValueError("Configuration base_urls URL must be a non-empty string")
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ("http", "https") and parsed.netloc and parsed.hostname
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Configuration base_urls URL must have an HTTP(S) scheme and host")

        request = data.get("request", {})
        if not isinstance(request, Mapping):
            raise ValueError("Configuration request must be a Mapping")
        if "timeout" in request:
            timeout = request["timeout"]
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
                raise ValueError("Configuration request.timeout must be a positive number")
        if "verify_ssl" in request and not isinstance(request["verify_ssl"], bool):
            raise ValueError("Configuration request.verify_ssl must be a bool")

        if not isinstance(data.get("default_headers", {}), Mapping):
            raise ValueError("Configuration default_headers must be a Mapping")

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
