# This Python file uses the following encoding: utf-8

"""百鬼棋局平安京阵容配置。"""

from tasks.Chess.strategy.lineup_strategy import build_lineup_strategy


PINGANJING_CONFIG = {
    'key': 'pinganjing',
    'display_name': '平安京',
    'secondary_bond': '追击',
    'hakuzosu_protect_position': 1,
    'shikigami_positions': {
        '青行灯': (1, 1, ()),
        '姑获鸟': (1, 2, ()),
        '御馔津': (1, 3, ()),
        '妖刀姬': (1, 4, ()),
        '花鸟卷': (1, 5, ()),
        '鬼切': (1, 6, ()),
        '夜叉': (1, 7, ()),
        '山兔': (1, 8, ()),
        '梦山白藏主': (1, 9, ('招财猫', '狰')),
    },
}


PINGANJING = build_lineup_strategy(PINGANJING_CONFIG)
