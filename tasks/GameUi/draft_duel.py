"""协同对弈返回町中：处理结算和离队确认，不重复点击被遮挡的返回键。"""

from module.base.timer import Timer
from module.exception import GamePageUnknownError
from tasks.DraftDuel.assets import DraftDuelAssets
from tasks.GameUi.assets import GameUiAssets
from tasks.GlobalGame.assets import GlobalGameAssets


def leave_draft_duel(task):
    timer = Timer(30).start()
    while not timer.reached():
        task.device.stuck_record_clear()
        task.screenshot()
        if task.appear(GameUiAssets.I_CHECK_TOWN):
            return True
        if task.appear(DraftDuelAssets.I_EXIT):
            task.appear_then_click(DraftDuelAssets.I_EXIT, interval=1)
        elif any(task.appear(rule) for rule in (
                DraftDuelAssets.I_EXIT_CANCEL, DraftDuelAssets.I_EXIT_MOMENT)):
            # 弹窗尚未完整出现时等待，不能继续点击背景中的黄色返回。
            pass
        elif any(task.appear(rule) for rule in (
                DraftDuelAssets.I_WIN_1, DraftDuelAssets.I_WIN_2,
                DraftDuelAssets.I_LOSS_2, DraftDuelAssets.I_SETTLEMENT_0_4,
                DraftDuelAssets.I_SGARED_ENDING_01, DraftDuelAssets.I_REWARD,
                DraftDuelAssets.I_DRAFT_SHARE, DraftDuelAssets.I_FIGHT_STATISTIC,
                DraftDuelAssets.I_FIGHT_RESULT)):
            task.click(DraftDuelAssets.C_CONTINUE, interval=1)
        else:
            task.appear_then_click(GlobalGameAssets.I_UI_BACK_YELLOW, interval=1)
        task.device.sleep(0.3)
    raise GamePageUnknownError('协同对弈离队后30秒内未确认返回町中')
