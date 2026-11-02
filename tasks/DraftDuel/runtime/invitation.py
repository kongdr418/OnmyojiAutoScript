"""协同对弈邀请、入队、返回大厅与匹配入口。"""

from module.base.timer import Timer
from module.logger import logger
from tasks.DraftDuel.config import DraftDuelConfig
from tasks.GameUi.page import page_draft_duel


class DraftDuelInvitationMixin:
    def _wait_member_selection(self, con: DraftDuelConfig) -> bool:
        """队员：等「等待中」消失，再等选人页；等待期间每秒清 stuck，避免 60 秒无操作被判卡死。"""
        wait_seconds = (con.limit_time.hour * 3600 + con.limit_time.minute * 60
                        + con.limit_time.second)
        timer = Timer(wait_seconds).start()
        while not timer.reached():
            self.device.stuck_record_clear()
            self.screenshot()
            if not self.appear(self.I_MEMBERS_AWAIT):
                break
            self.device.sleep(1.0)
        if self.appear(self.I_MEMBERS_AWAIT):
            return False

        timer = Timer(120).start()
        while not timer.reached():
            self.device.stuck_record_clear()
            self.screenshot()
            if self.appear(self.I_SELECTION_PAGE):
                return True
            self.device.sleep(1.0)
        return False

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

    def _ensure_draft_lobby(self) -> bool:
        """一局结束后可能已经回主界面：先导航回协同大厅。"""
        self.screenshot()
        if (self.appear(self.I_LOBBY_TITLE)
                or self.appear(self.I_NEW_MATCH)
                or self.appear(self.I_DRAFT_DUEL_COMPLETED_HALL)
                or self.appear(self.I_START)):
            return True
        logger.info('DraftDuel: not in draft lobby, goto draft duel page')
        try:
            self.goto_page(page_draft_duel)
        except Exception as e:
            logger.warning(f'DraftDuel: goto draft lobby failed: {e}')
            return False
        self.screenshot()
        return (self.appear(self.I_LOBBY_TITLE)
                or self.appear(self.I_NEW_MATCH)
                or self.appear(self.I_DRAFT_DUEL_COMPLETED_HALL)
                or self.appear(self.I_START))

    def _wait_selection_after_match(self, timeout: float = 180) -> bool:
        """点战后等选人页；匹配超时回大厅时重新点战。"""
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_SELECTION_PAGE):
                return True
            if self.appear(self.I_WAIT_FOR_MATCH):
                self.device.sleep(1.0)
                continue
            if self.appear(self.I_NEW_MATCH) and self.appear(self.I_START):
                logger.info('DraftDuel: match timeout, click start again')
                self.appear_then_click(self.I_START, interval=1)
                self.device.sleep(1.0)
                continue
            self.device.sleep(0.5)
        return False

    def _start_round(self) -> bool:
        """点「战」：新局先进选式神，已选好阵容则不再选。"""
        # 一局结束的奖励/番胜/共用结算面板可能盖住「新局」，先按图片逐页点掉
        for _ in range(8):
            self.screenshot()
            # 先按图片清结算/奖励弹窗，否则会盖住「新局」和加号
            if self._handle_settlement_pages():
                self.device.sleep(1.0)
                continue
            if self.appear(self.I_NEW_MATCH):
                break
            break

        if self.appear(self.I_INVITATION):
            logger.info('DraftDuel: re-invite teammate for next match')
            if not self._invite_teammate():
                logger.warning('DraftDuel: re-invite teammate failed')
                return False
            self.wait_until_appear(self.I_NEW_MATCH, wait_time=10)

        if self.appear(self.I_NEW_MATCH):
            logger.info('DraftDuel: new match, enter selection')
            if not self.appear_then_click(self.I_START, interval=1):
                logger.warning('DraftDuel: click start failed')
                return False
            if not self._wait_selection_after_match():
                logger.warning('DraftDuel: selection page not found')
                return False
            if not self._draft_phase():
                return False
            self._duel_share_write(self.picked_roles)
            self._battle_mate_names = self._duel_share_read_mate()
            # 详细页暂时不打开：速度/防御对上阵决策影响很小，名字用 picked_roles 即可
            # 后面要用时再放开 self._read_lineup_detail()
            return self._start_fielding()

        logger.warning('DraftDuel: unknown lobby state, stop')
        return False

    def _wait_accept_invite(self, con: DraftDuelConfig, timeout: float | None = None) -> bool:
        # 队员等邀请默认用 limit_time；第二局起只等 120 秒，队长不打就退出
        wait_seconds = (timeout if timeout is not None
                        else con.limit_time.hour * 3600 + con.limit_time.minute * 60
                        + con.limit_time.second)
        timer = Timer(wait_seconds).start()
        while True:
            self.device.stuck_record_clear()
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
