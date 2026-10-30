# This Python file uses the following encoding: utf-8
import re

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

    AVAILABLE_AMOUNTS = ('30万', '300000')
    FULL_AMOUNTS = ('100000', '10万')
    # 一局协同对弈双方一共选 6 位式神
    TOTAL_PICKS = 6

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

        if con.start_enable:
            if not self._start_round():
                logger.warning('DraftDuel: start round failed')
                self.set_next_run(task='DraftDuel', success=False, finish=True)
                raise TaskEnd('DraftDuel')
            logger.info('DraftDuel: round stopped, battle loop is not implemented yet')
        else:
            logger.info('DraftDuel: start disabled, stop after team formed')

        self.set_next_run(task='DraftDuel', success=True, finish=True)
        raise TaskEnd('DraftDuel')

    def _run_member(self, con: DraftDuelConfig):
        # 队员不用进大厅，原地等队长的组队邀请
        logger.info('DraftDuel: wait for team invite')
        if not self._wait_accept_invite(con):
            logger.warning('DraftDuel: no invite to accept')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        logger.info('DraftDuel: joined team, waiting for leader')
        self.screenshot()
        if not self.appear(self.I_MEMBERS_AWAIT):
            logger.info('DraftDuel: member lobby not found, leader may already start')
            self.set_next_run(task='DraftDuel', success=True, finish=True)
            raise TaskEnd('DraftDuel')

        # 队员只有「等待中」，不能提前结束，否则调度器会切回主界面解散队伍
        wait_seconds = con.limit_time.hour * 3600 + con.limit_time.minute * 60 + con.limit_time.second
        self.wait_until_disappear(self.I_MEMBERS_AWAIT, timeout=wait_seconds)
        self.screenshot()
        if self.appear(self.I_MEMBERS_AWAIT):
            logger.warning('DraftDuel: leader did not start in time')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        logger.info('DraftDuel: leader started, entering selection')
        if not self.wait_until_appear(self.I_SELECTION_PAGE, wait_time=15):
            logger.warning('DraftDuel: selection page not found')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')
        if not self._draft_phase():
            logger.warning('DraftDuel: member selection failed')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        logger.info('DraftDuel: member selection finished')
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
        if '免费' in text:
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
        # 好友面板加载完成后才会出现邀请按钮
        if not self.wait_until_appear(self.I_INVITE_ENSURE, wait_time=10):
            return False
        self.wait_until_disappear(self.I_I_LOAD, timeout=5)
        return True

    def _wait_teammate(self) -> bool:
        wait_time = self.config.draft_duel.invite_config.wait_time_v
        self.wait_until_disappear(self.I_INVITATION, timeout=wait_time.total_seconds())
        self.screenshot()
        if self.appear(self.I_INVITATION):
            logger.warning('DraftDuel: wait teammate timeout')
            return False
        logger.info('DraftDuel: teammate joined')
        return True

    def _start_round(self) -> bool:
        """点「战」：新局先进选式神，已选好阵容则不再选。"""
        self.screenshot()
        if self.appear(self.I_NEW_MATCH):
            logger.info('DraftDuel: new match, enter selection')
            if not self.appear_then_click(self.I_START, interval=1):
                logger.warning('DraftDuel: click start failed')
                return False
            if not self.wait_until_appear(self.I_SELECTION_PAGE, wait_time=15):
                logger.warning('DraftDuel: selection page not found')
                return False
            return self._draft_phase()

        if self.appear(self.I_DRAFT_DUEL_COMPLETED_HALL) and \
                (self.appear(self.I_LOSS_0) or self.appear(self.I_LOSS_1)):
            logger.info('DraftDuel: lineup ready, battle flow is not implemented yet')
            return True

        logger.warning('DraftDuel: unknown lobby state, stop')
        return False

    def _draft_phase(self) -> bool:
        """选满 6 位式神：优先跟队友推荐，没有推荐时走策略。"""
        logger.hr('Draft selection', 2)
        picked = set()
        timer = Timer(300).start()
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_DRAFT_DUEL_COMPLETED_HALL):
                logger.info('DraftDuel: selection finished')
                return True
            if not self.appear(self.I_SELECTION_PAGE):
                self.device.sleep(0.5)
                continue

            role_text = self.O_HINT_ROLE.ocr(self.device.image)
            time_text = self.O_HINT_TIME.ocr(self.device.image)
            index = self._parse_pick_index(role_text)
            seconds = self._parse_seconds(time_text)
            logger.info(
                f'DraftDuel: pick={index}, hint=[{role_text}] [{time_text}], {seconds}s'
            )

            if not 1 <= index <= self.TOTAL_PICKS or index in picked:
                self.device.sleep(0.5)
                continue
            # 队友的推荐式神只在倒计时 30s~25s 内看
            if not 25 <= seconds <= 30:
                self.device.sleep(0.5)
                continue

            choice = self._recommended_choice()
            if choice is None:
                choice = self._choose_by_strategy()
            if choice is None:
                logger.warning('DraftDuel: no recommendation and no pick strategy')
                return False

            self.click(choice, interval=1)
            logger.info(f'DraftDuel: pick {index} -> {choice.name}')
            if not self._wait_pick_change(index):
                logger.warning('DraftDuel: pick did not advance, retry')
                self.device.sleep(1)
                continue
            picked.add(index)
            self.device.sleep(1)
        logger.warning('DraftDuel: selection timeout')
        return False

    def _wait_pick_change(self, index: int, timeout: float = 3) -> bool:
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            text = self.O_HINT_ROLE.ocr(self.device.image)
            if self._parse_pick_index(text) != index:
                return True
            self.device.sleep(0.5)
        return False

    def _recommended_choice(self):
        matches = self.I_RECOMMEND_ROLE.match_all_any(
            self.device.image, frame_id=self.device.image_frame_id
        )
        if not matches:
            return None
        # 匹配结果是 (score, x, y, w, h)，按 x 中心归到左中右三张卡
        slot_centers = (273, 648, 1023)
        choices = (self.C_CHOOSE_1, self.C_CHOOSE_2, self.C_CHOOSE_3)
        for _, x, y, w, h in matches:
            center = x + w / 2
            slot = min(range(3), key=lambda i: abs(center - slot_centers[i]))
            if abs(center - slot_centers[slot]) <= 150:
                return choices[slot]
        return None

    def _choose_by_strategy(self):
        """队友没有推荐时的选式神策略，待定。"""
        return None

    @staticmethod
    def _parse_pick_index(text: str) -> int:
        match = re.search(r'第\s*(\d+)', text or '')
        if match:
            return int(match.group(1))
        nums = re.findall(r'\d+', text or '')
        return int(nums[0]) if nums else 0

    @staticmethod
    def _parse_seconds(text: str) -> int:
        match = re.search(r'(\d+)\s*s', text or '', re.IGNORECASE)
        if match:
            return int(match.group(1))
        nums = re.findall(r'\d+', text or '')
        return int(nums[-1]) if nums else 0

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
            self.device.sleep(1)

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
            self.device.sleep(0.5)
        return not self.appear_accept()

    def _switch_friend_class(self, index: int) -> None:
        """覆盖 GeneralInvite 的页签切换。"""
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
