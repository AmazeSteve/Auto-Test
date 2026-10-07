import json


class ResponseParser:
    """
    统一响应解析器。

    目标：
    1. 普通 JSON：
       {"code": 200}
       -> dict

    2. JSON 字符串包 JSON：
       "{\"total\":42,\"rows\":[]}"
       -> dict

    3. JSON 字符串包 JSON 字符串：
       最多递归解析指定层数

    4. 非 JSON 响应：
       抛出明确异常，由调用方决定是否使用 text/regex。
    """

    DEFAULT_MAX_DEPTH = 3

    @classmethod
    def parse_json(cls, response, max_depth: int = DEFAULT_MAX_DEPTH):
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError(
                "响应不是合法 JSON，"
                f"status_code={response.status_code}, "
                f"response={response.text[:500]}"
            ) from exc

        depth = 0

        while isinstance(data, str) and depth < max_depth:
            text = data.strip()

            # 只有看起来仍然像 JSON object / array 时才继续解析
            if not (
                (text.startswith("{") and text.endswith("}"))
                or
                (text.startswith("[") and text.endswith("]"))
            ):
                break

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                break

            depth += 1

        return data