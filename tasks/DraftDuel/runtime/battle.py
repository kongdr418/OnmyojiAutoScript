"""战斗等待、胜负结算及后续小局调度。"""

import time

from module.base.timer import Timer
from module.logger import logger


class DraftDuelBattleMixin:
    def _run_finished(self) -> bool:
        return getattr(self, '_run_wins', 0) >= 6 or getattr(self, '_run_losses', 0) >= 2

    def _count_result(self, result: str):
        if getattr(self, '_result_counted', False):
            return
        self._result_counted = True
        self._battle_count = getattr(self, '_battle_count', 0) + 1
        if result == 'win':
            self._run_wins = getattr(self, '_run_wins', 0) + 1
        else:
            self._run_losses = getattr(self, '_run_losses', 0) + 1
        logger.info(f'DraftDuel: result {self._run_wins}W{self._run_losses}L')
        if self._run_finished():
            # 6 胜或 2 负：先标记本局结束，避免再去点“战”开下一场
            self._match_ended = True

    def _click_rule(self, rule, prefix: str) -> bool:
        """按规则中心点击，但每次用唯一 control_name，避免连点保护。"""
        x, y, w, h = rule.roi_front
        name = f'{prefix}_{int(time.monotonic() * 1000) % 1000000}'
        return self.device.click(x + w // 2, y + h // 2, control_name=name)

    def _handle_settlement_pages(self) -> bool:
        """胜利/失败/共用结算/奖励页逐页点 continue。

        胜利页 1 可能不出现；出现则后面一定跟胜利页 2。
        失败只有失败页 2。最后是共用结算页和奖励页。
        """
        if self.appear(self.I_WIN_1):
            self._count_result('win')
            self._click_rule(self.C_CONTINUE, 'dd_continue')
            return True
        if self.appear(self.I_WIN_2):
            self._count_result('win')
            self._click_rule(self.C_CONTINUE, 'dd_continue')
            return True
        if self.appear(self.I_LOSS_2):
            self._count_result('loss')
            self._click_rule(self.C_CONTINUE, 'dd_continue')
            return True
        if self.appear(self.I_SETTLEMENT_0_4):
            # 两个红 X 的结算页：已经二败出局；阈值 0.95 避免大厅 0-4 番胜误匹配
            if getattr(self, '_run_losses', 0) < 2:
                self._run_losses = 2
            self._match_ended = True
            logger.info(f'DraftDuel: 0-4 settlement -> 2 losses, {self._run_wins}W2L')
            self._click_rule(self.C_CONTINUE, 'dd_continue')
            return True
        if self.appear(self.I_SGARED_ENDING_01):
            self._click_rule(self.C_CONTINUE, 'dd_continue')
            return True
        if self.appear(self.I_REWARD):
            self._reward_seen = True
            self._click_rule(self.C_CONTINUE, 'dd_continue')
            return True
        return False

    def _clear_final_panel(self) -> bool:
        """大厅 1 番胜/两个 X 面板：点一次继续。"""
        self.screenshot()
        if self.appear(self.I_SETTLEMENT_0_4):
            logger.info('DraftDuel: final lobby panel, click continue')
            self._click_rule(self.C_CONTINUE, 'dd_continue')
            self.device.sleep(1.0)
            return True
        return False

    def _dismiss_settlement(self) -> bool:
        """结算页：优先按胜利/失败/共用结算/奖励图片点 continue，最后 OCR 兜底。"""
        if self._handle_settlement_pages():
            return True
        try:
            results = self.O_SCREEN.detect_and_ocr(self.device.image)
        except Exception:
            results = []
        for item in results:
            text = str(item.ocr_text or '').strip()
            if text in ('确定', '確定', '点击屏幕继续', '點擊屏幕繼續'):
                x, y = item.box.mean(axis=0)
                self.device.click(int(x), int(y),
                                  control_name=f'dd_settle_ok_{int(time.monotonic()*1000)%100000}')
                return True
        self._click_rule(self.C_SETTLEMENT_FALLBACK, 'dd_settle_fallback')
        return False

    def _wait_battle_return(self, timeout: float = 3600) -> bool:
        """打完一局（6胜或2负）：处理上阵、结算、下一场战斗。"""
        names = getattr(self, '_battle_detail', None) or self.picked_roles
        timer = Timer(timeout).start()
        while not timer.reached():
            self.device.stuck_record_clear()
            self.screenshot()
            auto_ready = self._ensure_lobby_auto_form()
            if self._run_finished() and (getattr(self, '_reward_seen', False)
                                         or getattr(self, '_match_ended', False)):
                return True
            if self.appear(self.I_BATTLE_PAGE):
                self._battle_fielded = False
                self.device.sleep(0.5)
                continue

            if self._handle_settlement_pages():
                if self._run_finished() and (getattr(self, '_reward_seen', False)
                                             or getattr(self, '_match_ended', False)):
                    return True
                continue

            if self._prep_visible(full_fallback=False):
                self._result_counted = False
                if not getattr(self, '_battle_fielded', False):
                    self._battle_fielded = True
                    if self.AUTO_FORM_AFTER_FIRST and getattr(self, '_battle_count', 0) >= 1:
                        if not self._auto_form_field():
                            return False
                    elif not self._field_battle_lineup(names):
                        logger.warning('DraftDuel: fielding failed')
                        return False
                self.device.sleep(1.0)
                continue
            self._battle_fielded = False

            if self.appear(self.I_START):
                if not auto_ready:
                    if self._auto_wait_timer is None:
                        self._auto_wait_timer = Timer(600).start()
                    if self._auto_wait_timer.reached():
                        logger.warning('DraftDuel: automatic deployment readiness timeout')
                        return False
                    self.device.sleep(0.5)
                    continue
                self._auto_wait_timer = None
                if self._configured_role() == 'captain':
                    self._result_counted = False
                    self._battle_fielded = False
                    self.appear_then_click(self.I_START, interval=1)
                self.device.sleep(0.5)
                continue
            self.device.sleep(0.5)
        logger.warning('DraftDuel: battle timeout')
        return self._run_finished()
