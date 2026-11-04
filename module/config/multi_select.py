"""多选输入兼容：原生列表、JSON列表、单项及旧文本分隔符。"""
import json
import re


def normalize_multi_select(value):
    if isinstance(value, str):
        value = value.strip()
        if value.startswith(('[', '"')):
            decoded = json.loads(value)
            return normalize_multi_select(decoded)
        return [item.strip() for item in re.split(r'[,，;；\n\r]+', value) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(normalize_multi_select(item))
        return result
    raise ValueError('多选项必须为列表或选项字符串')
