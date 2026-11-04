from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.activity_handlers import handle_activity_close

"""式神活动统一页面定义。"""

import time

from module.base.timer import Timer
from module.exception import GamePageUnknownError
from module.logger import logger
from tasks.ActivityShikigami.assets import ActivityShikigamiAssets
from tasks.Component.RightActivity.assets import RightActivityAssets
from tasks.GameUi.page import (
    Page,
    all_of,
    any_of,
    conditional_action,
    page_battle,
    page_battle_prepare,
    page_battle_result,
    page_main,
    page_reward,
    page_shikigami_records,
)
from tasks.GlobalGame.assets import GlobalGameAssets


ACTIVITY_COLUMN_SWITCH_MAX_TRIES = 8
ACTIVITY_AUXILIARY_SETTLE_SECONDS = 1.0


def _settle_activity_auxiliary(task):
    """附属界面出现后等待一秒，再用新截图确认并处理。"""
    time.sleep(ACTIVITY_AUXILIARY_SETTLE_SECONDS)
    task.screenshot()


def find_activity_entry(task) -> bool:
    """在庭院右侧栏目中寻找本期式神活动入口。"""
    switched = 0
    for _ in range(ACTIVITY_COLUMN_SWITCH_MAX_TRIES):
        task.screenshot()
        if not task.appear(task.I_CHECK_MAIN):
            return False
        if task.appear(ActivityShikigamiAssets.I_MAIN_GOTO_ACTIVITY):
            return True
        if task.appear(RightActivityAssets.I_TOGGLE_BUTTON):
            _settle_activity_auxiliary(task)
            if task.appear_then_click(RightActivityAssets.I_TOGGLE_BUTTON, interval=0):
                switched += 1
        time.sleep(0.5)

    task.screenshot()
    if task.appear(ActivityShikigamiAssets.I_MAIN_GOTO_ACTIVITY):
        return True
    logger.warning(
        f'ActivityShikigami entry not found after switching columns {switched} times '
        f'({ACTIVITY_COLUMN_SWITCH_MAX_TRIES} checks)'
    )
    raise GamePageUnknownError('Cannot find ActivityShikigami entry')


def handle_activity_reward(task) -> bool:
    if not task.appear(GlobalGameAssets.I_UI_REWARD):
        return False
    _settle_activity_auxiliary(task)
    if not task.appear(GlobalGameAssets.I_UI_REWARD):
        return False
    logger.info('Clear activity reward page via ui_reward')
    task.click(GlobalGameAssets.C_UI_REWARD, interval=0)
    task.device.click_record_clear()
    return True


# 保留旧导出名，跳过/确定/红叉统一走活动全局附属处理。
handle_activity_story = handle_activity_close


def handle_activity_overlay(task) -> bool:
    """清理活动奖励、签到等附属页面，并等待活动主页稳定。"""
    timer = Timer(15).start()
    award_clicked = False
    while not timer.reached():
        task.screenshot()

        if task.appear(GlobalGameAssets.I_UI_REWARD):
            if not award_clicked:
                _settle_activity_auxiliary(task)
                if task.appear(GlobalGameAssets.I_UI_REWARD):
                    logger.info('Clear activity award overlay via ui_reward')
                    task.click(GlobalGameAssets.C_UI_REWARD, interval=0)
                    task.device.click_record_clear()
                    award_clicked = True
            time.sleep(0.2)
            continue

        if handle_activity_close(task):
            continue

        if task.appear(ActivityShikigamiAssets.I_PAGE_CHECK_ACTIVITY):
            # 主界面标志可能先于延迟弹窗出现，等待一秒后再确认稳定。
            _settle_activity_auxiliary(task)
            if task.appear(GlobalGameAssets.I_UI_REWARD) or \
                    task.appear(ActivityShikigamiAssets.I_ACTIVITY_GENERAL_CLOSE_RED):
                continue
            if task.appear(ActivityShikigamiAssets.I_PAGE_CHECK_ACTIVITY):
                logger.info('ActivityShikigami main page ready')
                return True

        time.sleep(0.2)

    logger.warning('ActivityShikigami overlays did not finish within 15s')
    return False


# 活动页面、入口及附属弹窗恢复统一由 GameUi 管理。
page_act = Page(any_of(ActivityShikigamiAssets.I_PAGE_CHECK_ACTIVITY, GlobalGameAssets.I_UI_REWARD), priority=70, key='page_act', name='page_act', category='activity_shikigami')
page_act.add_enter_success_hooks(handle_activity_overlay)
page_act.add_enter_failure_hooks(
    find_activity_entry,
    handle_activity_reward,
    handle_activity_close,
)
page_act.connect(page_main, GlobalGameAssets.I_UI_BACK_YELLOW, key='activity->main')
page_main.connect(page_act, ActivityShikigamiAssets.I_MAIN_GOTO_ACTIVITY, key='main->activity')

# 当期爬塔入口中间层。
PAGE_ACT_GENERAL_1 = Page(ActivityShikigamiAssets.I_PAGE_CHECK_ACT_GENERAL_1, key='PAGE_ACT_GENERAL_1', name='PAGE_ACT_GENERAL_1', category='activity_shikigami')
# 兼容旧页面引用；同一个页面对象，不重复注册。
page_activity_exploration = PAGE_ACT_GENERAL_1
page_act.connect(PAGE_ACT_GENERAL_1, ActivityShikigamiAssets.I_TO_GENERAL_1,
                 key='activity->general_1')
PAGE_ACT_GENERAL_1.connect(page_act, ActivityShikigamiAssets.I_UI_BACK_GENERAL_1,
                                  key='exploration->activity')

PAGE_ACT_GENERAL_2 = Page(ActivityShikigamiAssets.I_PAGE_CHECK_ACT_GENERAL_2, key='PAGE_ACT_GENERAL_2', name='PAGE_ACT_GENERAL_2', category='activity_shikigami')
page_climb_main = PAGE_ACT_GENERAL_2
page_climb_main.connect(page_act, GlobalGameAssets.I_UI_BACK_YELLOW, key='climb_main->activity')

# 当期爬塔四种战斗页面。
page_climb_ap = Page(all_of(ActivityShikigamiAssets.I_PAGE_CHECK_BATTLE_AP, ActivityShikigamiAssets.I_CLIMB_MODE_AP), key='page_climb_ap', name='page_climb_ap', category='activity_shikigami')
page_climb_ap.connect(page_act, GlobalGameAssets.I_UI_BACK_YELLOW, key='climb_ap->activity')

page_climb_pass = Page(all_of(ActivityShikigamiAssets.I_PAGE_CHECK_BATTLE_PASS, ActivityShikigamiAssets.I_CLIMB_MODE_PASS), key='page_climb_pass', name='page_climb_pass', category='activity_shikigami')
page_climb_pass.connect(PAGE_ACT_GENERAL_1, GlobalGameAssets.I_UI_BACK_YELLOW, key='climb_pass->general_1')

page_climb_ap100 = Page(ActivityShikigamiAssets.I_CLIMB_MODE_AP100, key='page_climb_ap100', name='page_climb_ap100', category='activity_shikigami')
page_climb_ap100.add_enter_failure_hooks(handle_activity_close)
page_climb_ap100.connect(page_act, GlobalGameAssets.I_UI_BACK_YELLOW, key='climb_ap100->activity')

page_climb_boss = Page(ActivityShikigamiAssets.I_AS_BOSS_FIRE, key='page_climb_boss', name='page_climb_boss', category='activity_shikigami')
page_climb_boss.connect(page_act, GlobalGameAssets.I_UI_BACK_YELLOW, key='climb_boss->activity')

# 当前版本：主页面直达体力、百体、首领和通用页面 1；门票从通用页面 1 进入。
page_act.connect(page_climb_ap, ActivityShikigamiAssets.I_TO_BATTLE_AP,
                 key='activity->climb_ap')
page_act.connect(page_climb_ap100, ActivityShikigamiAssets.I_TO_BATTLE_AP100,
                 key='activity->climb_ap100')
page_act.connect(page_climb_boss, ActivityShikigamiAssets.I_TO_BATTLE_BOSS,
                 key='activity->climb_boss')
PAGE_ACT_GENERAL_1.connect(page_climb_pass, ActivityShikigamiAssets.I_GENERAL_1_TO_CLIMB,
                          key='general_1->climb_pass')


# 大富翁棋盘。
page_rich_man = Page(ActivityShikigamiAssets.I_PAGE_CHECK_RICHMAN, key='page_rich_man', name='page_rich_man', category='activity_shikigami')
page_rich_man.connect(page_act, GlobalGameAssets.I_UI_BACK_YELLOW, key='rich_man->activity')

# 伪神降临沿用旧素材作为占位；下次复刻时整体替换 fakegod 子目录。
page_fakegod_action = Page(ActivityShikigamiAssets.I_FG_CLIMB_MODE_PASS, key='page_fakegod_action', name='page_fakegod_action', category='activity_shikigami')
page_fakegod_action.connect(page_act, GlobalGameAssets.I_UI_BACK_YELLOW, key='fakegod_action->activity')


# 每次到达活动子页面先清理同类红叉；失败恢复也复用同一资源。
for _activity_page in (page_activity_exploration, page_climb_main, page_climb_ap,
                       page_climb_pass, page_climb_ap100, page_climb_boss,
                       page_rich_man, page_fakegod_action):
    _activity_page.add_enter_success_hooks(handle_activity_close)
    if handle_activity_close not in _activity_page.on_enter_failure:
        _activity_page.add_enter_failure_hooks(handle_activity_close)
