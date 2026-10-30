# This Python file uses the following encoding: utf-8
import re
from time import sleep

from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger

from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.DraftDuel.assets import DraftDuelAssets
from tasks.DraftDuel.config import DraftDuelConfig, UserStatus
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_draft_duel


class ScriptTask(GameUi, GeneralInvite, DraftDuelAssets):
    """协同对弈（Draft Duel）。"""

    AVAILABLE_TEXTS = ('本次免费',)
    AVAILABLE_AMOUNTS = ('30万', '300000')
    FULL_AMOUNTS = ('100000', '10万')

    def run(self):
        con: DraftDuelConfig = self.config.draft_duel.draft_duel_config
        logger.hr('Draft Duel', 2)

        if con.user_status == UserStatus.MEMBER:
            self._run_member(con)
        else:
            self._run_leader(con)

    def _run_leader(self, con: DraftDuelConfig):
        self.goto_page(page_draft_duel)
        logger.info('DraftDuel: lobby reached')
        logger.info(f'DraftDuel: limit_time={con.limit_time}')

        state = self._cost_state()
        if state == 'full':
            logger.info('DraftDuel: weekly attempts are full')
            self.set_next_run(task='DraftDuel', success=True, finish=True)
            raise TaskEnd('DraftDuel')
        if state == 'unknown':
            logger.warning('DraftDuel: unrecognized cost text, stop without entering a match')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        if not self._invite_teammate():
            logger.warning('DraftDuel: invite friend failed')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        logger.info('DraftDuel: attempts available, flow after lobby is not implemented yet')
        self.set_next_run(task='DraftDuel', success=True, finish=True)
        raise TaskEnd('DraftDuel')

    def _run_member(self, con: DraftDuelConfig):
        # 队员不用进大厅，原地等队长的组队邀请
        logger.info('DraftDuel: wait for team invite')
        if not self._wait_accept_invite(con):
            logger.warning('DraftDuel: no invite to accept')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        logger.info('DraftDuel: joined team, flow after lobby is not implemented yet')
        self.set_next_run(task='DraftDuel', success=True, finish=True)
        raise TaskEnd('DraftDuel')

    def _cost_state(self) -> str:
        text = self.O_GOLD.ocr(self.device.image)
        state = self._parse_cost_text(text)
        logger.info(f'DraftDuel: cost OCR [{text}] -> {state}')
        return state

    @classmethod
    def _parse_cost_text(cls, text: str) -> str:
        text = (text or '').replace(' ', '').replace('\u3000', '')
        if text in cls.AVAILABLE_TEXTS:
            return 'available'
        # 去掉标点，只留数字和万/亿
        amount = re.sub(r'[^\d万亿]', '', text)
        if amount in cls.AVAILABLE_AMOUNTS:
            return 'available'
        if amount in cls.FULL_AMOUNTS:
            return 'full'
        return 'unknown'

    def _invite_teammate(self) -> bool:
        """点大厅的邀请位，复用 GeneralInvite 的好友面板逻辑。"""
        config = self.config.draft_duel.invite_config
        if not config.friend_list_v:
            logger.info('DraftDuel: no friend configured, skip invite')
            return True

        self.screenshot()
        if not self.appear(self.I_INVITATION):
            logger.warning('DraftDuel: invite button not found')
            return False
        if not self.appear_then_click(self.I_INVITATION, interval=1):
            return False
        if not self._wait_invite_panel():
            logger.warning('DraftDuel: invite panel not found')
            return False
        if not self.invite_friends(config, open_invite=False):
            logger.warning('DraftDuel: invite friend failed')
            return False
        return self._wait_teammate()

    def _wait_invite_panel(self) -> bool:
        timer = Timer(10).start()
        while True:
            self.screenshot()
            if self.appear(self.I_LOAD_FRIEND) or self.appear(self.I_INVITE_ENSURE):
                self.wait_until_disappear(self.I_I_LOAD, timeout=5)
                return True
            if timer.reached():
                return False
            sleep(0.5)

    def _wait_teammate(self) -> bool:
        wait_time = self.config.draft_duel.invite_config.wait_time_v
        timer = Timer(wait_time.total_seconds()).start()
        while True:
            self.screenshot()
            if not self.appear(self.I_INVITATION):
                logger.info('DraftDuel: teammate joined')
                return True
            if timer.reached():
                logger.warning('DraftDuel: wait teammate timeout')
                return False
            sleep(1)

    def _wait_accept_invite(self, con: DraftDuelConfig) -> bool:
        # 队员等邀请的时间用 limit_time，避免队长还没准备好就退出
        wait_seconds = con.limit_time.hour * 3600 + con.limit_time.minute * 60 + con.limit_time.second
        timer = Timer(wait_seconds).start()
        while True:
            self.screenshot()
            if self.appear_accept():
                logger.info('DraftDuel: invite popup found')
                return self._accept_invite_popup()
            if timer.reached():
                return False
            sleep(1)

    def _accept_invite_popup(self) -> bool:
        timer = Timer(30).start()
        while not timer.reached():
            self.screenshot()
            if not self.appear_accept() and not self.appear(self.I_GI_SURE):
                return True
            if self.appear_then_click(self.I_I_NO_DEFAULT, interval=1):
                continue
            if self.appear_then_click(self.I_GI_SURE, interval=1):
                continue
            if self.appear_then_click(self.I_I_ACCEPT_DEFAULT, interval=1):
                continue
            if self.appear_then_click(self.I_I_ACCEPT, interval=1) or \
                    self.appear_then_click(self.I_I_ACCEPT_APPRENTICE, interval=1):
                continue
            sleep(0.5)
        return not self.appear_accept()

    def _switch_friend_class(self, index: int) -> None:
        """覆盖 GeneralInvite 的页签切换。

        共享的 I_FLAG_2_ON 坐标 (472,82) 在页签上方，切换「最近」会卡死在
        ui_click 的无限循环里；这里改用「暗色页签消失」判断是否选中。
        """
        flags = (self.I_FLAG_1_OFF, self.I_FLAG_2_OFF, self.I_FLAG_3_OFF, self.I_FLAG_4_OFF)
        if not 0 <= index < len(flags):
            return
        flag = flags[index]
        timer = Timer(8).start()
        while not timer.reached():
            self.screenshot()
            if not self.appear(flag):
                return
            self.appear_then_click(flag, interval=1)
        logger.warning(f'DraftDuel: switch friend class {index} timeout')
