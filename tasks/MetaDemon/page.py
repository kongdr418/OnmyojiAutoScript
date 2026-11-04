from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.page import (
    Page,
    any_of,
    conditional_action,
    page_act_list,
    page_battle,
    page_battle_result,
    page_main,
    page_reward,
    page_shikigami_records,
    random_click,
)
from tasks.GlobalGame.assets import GlobalGameAssets
from tasks.MetaDemon.assets import MetaDemonAssets
from tasks.Component.RightActivity.assets import RightActivityAssets


def find_activity_entry(task):
    """在庭院侧栏寻找本期活动入口；每次切换后重新截图。"""
    import time

    for _ in range(8):
        task.screenshot()
        if not task.appear(task.I_CHECK_MAIN):
            return False
        if task.appear(MetaDemonAssets.I_MAIN_GOTO_ACTIVITY):
            return True
        task.appear_then_click(RightActivityAssets.I_TOGGLE_BUTTON, interval=0.5)
        time.sleep(0.5)
    return False

page_act_list_meta_demon = Page(MetaDemonAssets.I_CHECK_ACT_LIST_METADEMON_ACT)
page_act_list.connect(page_act_list_meta_demon, MetaDemonAssets.L_GOTO_METADEMON_LIST, key="page_act_list->page_act_list_meta_demon")
page_act_list_meta_demon.connect(page_main, GlobalGameAssets.I_UI_BACK_YELLOW, key="page_act_list_meta_demon->page_main")

page_meta_demon = Page(MetaDemonAssets.I_MD_CHECK_MAIN_PAGE)
page_meta_demon.add_enter_success_hooks(MetaDemonAssets.I_MD_GET_YESTERDAY_REWARD, MetaDemonAssets.I_MD_CLOSE_POPUP)
page_meta_demon.add_enter_failure_hooks(
    find_activity_entry,
    conditional_action(GlobalGameAssets.I_UI_REWARD, random_click),
    GlobalGameAssets.I_UI_BACK_RED,
)
page_main.connect(page_meta_demon, MetaDemonAssets.I_MAIN_GOTO_ACTIVITY, key="page_main->page_meta_demon")
page_meta_demon.connect(page_main, GlobalGameAssets.I_UI_BACK_YELLOW, key="page_meta_demon->page_main")
page_act_list_meta_demon.connect(page_meta_demon, GameUiAssets.I_ACT_LIST_GOTO_ACT, key="page_act_list_meta_demon->page_meta_demon")

page_meta_demon_boss = Page(MetaDemonAssets.I_CHECK_BOSS_PAGE)
page_meta_demon_boss.add_enter_success_hooks(MetaDemonAssets.I_MD_CLOSE_POPUP)
page_meta_demon_boss.connect(page_meta_demon, GlobalGameAssets.I_UI_BACK_YELLOW, key="page_meta_demon_boss->page_meta_demon")
page_meta_demon.connect(page_meta_demon_boss, MetaDemonAssets.I_MD_CHECK_MAIN_PAGE, key="page_meta_demon->page_meta_demon_boss")

page_reward.connect(page_meta_demon_boss, random_click(), key="page_reward->page_meta_demon_boss")
page_battle_result.connect(page_meta_demon_boss, random_click(), key="page_battle_result->page_meta_demon_boss")
