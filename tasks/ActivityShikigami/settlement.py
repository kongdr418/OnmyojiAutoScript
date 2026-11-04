"""复用本项目的左右安全结算区域，避免修改其他任务的点击策略。"""
from copy import copy

from tasks.GameUi.default_pages import random_click


def settlement_random_click():
    return copy(random_click())
