"""式神活动及子页面共用的附属弹窗处理。"""

import time

from module.exception import GameStuckError
from module.logger import logger
from tasks.ActivityShikigami.assets import ActivityShikigamiAssets as GameUiAssets


def handle_activity_overlays(task) -> bool:
    """活动通用附属处理：跳过确认、剧情跳过、红叉；每次点击后重新截图。"""
    rules = (
        GameUiAssets.I_ACTIVITY_SKIP_ENSURE,
        GameUiAssets.I_ACTIVITY_SKIP,
        GameUiAssets.I_ACTIVITY_GENERAL_CLOSE_RED,
    )
    clicked = False
    for _ in range(6):
        task.screenshot()
        rule = next((rule for rule in rules if task.appear(rule)), None)
        if rule is None:
            return clicked
        time.sleep(0.2)
        task.screenshot()
        if not task.appear_then_click(rule, interval=0):
            continue
        logger.info(f'Handle ActivityShikigami overlay: {rule.name}')
        clicked = True
        task.device.click_record_clear()
        time.sleep(0.2)
    task.screenshot()
    if any(task.appear(rule) for rule in rules):
        raise GameStuckError('Activity overlay remains after six attempts')
    return clicked


# Existing activity callers and page hooks share the same global overlay pipeline.
handle_activity_close = handle_activity_overlays
