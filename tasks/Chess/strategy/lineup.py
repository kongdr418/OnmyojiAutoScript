# This Python file uses the following encoding: utf-8

"""百鬼棋局阵容羁绊注册表。

新增体系时：
在 ``lineup_strategy.json`` 中维护阵容，由 ``lineup_loader.py`` 读取。

OASX 下拉选项和主任务运行时选择均由本注册表生成。
"""

from enum import Enum

from tasks.Chess.strategy.lineup_loader import load_lineup_registry


LINEUP_REGISTRY = load_lineup_registry()

DEFAULT_LINEUP_KEY = 'qijiaoshan'

# 枚举值使用中文，使 OASX 下拉框直接显示中文；运行时通过下面的解析
# 函数还原成稳定的拼音键。
LEGACY_LINEUP_NAMES = {'pinganjing': '平安京', 'qijiaoshan': '七角山',
                       'minghuo': '冥火'}
LineupBond = Enum(
    'LineupBond',
    {key.upper(): name for key, name in LEGACY_LINEUP_NAMES.items()}
    | {key.upper(): entry['display_name'] for key, entry in LINEUP_REGISTRY.items()
       if key not in LEGACY_LINEUP_NAMES}
    | {'CUSTOM': '自定义阵容'}, type=str,
)


def resolve_lineup_key(value) -> str:
    """将拼音键、中文显示名或 OASX 枚举值统一解析为拼音键。"""
    raw = getattr(value, 'value', value)
    raw = str(raw or '').strip()
    registry = load_lineup_registry()
    for key, name in LEGACY_LINEUP_NAMES.items():
        if raw == name:
            return key
    if raw in registry:
        return raw
    for key, entry in registry.items():
        if raw == entry['display_name']:
            return key
    return DEFAULT_LINEUP_KEY


def oasx_lineup_choices() -> dict[str, str]:
    """Build fresh display labels with stable source/key identities for OASX."""
    from tasks.Chess.strategy.custom_lineups import read_lineups
    choices = {}
    for source, label, records in (
        ('builtin', '内置', load_lineup_registry()),
        ('custom', '自定义', read_lineups()),
    ):
        for key, record in records.items():
            choices[f'{label} · {record["display_name"]} [{key}]'] = f'{source}:{key}'
    return choices
