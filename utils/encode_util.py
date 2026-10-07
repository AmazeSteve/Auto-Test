import base64

"""
加密解密工具
"""
class EncodeUtil:

    """
    编码 为base64

    """
    @staticmethod
    def base64_encode(value: str,encoding:str = 'utf-8') -> str:
        if not isinstance(value, str):
            raise TypeError("text必须是字符串")
        return base64.b64encode(value.encode(encoding)).decode('ascii')