"""协同对弈任务生命周期、开放时间及队长/队员循环。"""

import time
from datetime import datetime, timedelta

from module.exception import TaskEnd
from module.logger import logger
from tasks.DraftDuel.config import DraftDuelConfig
from tasks.GameUi.page import page_draft_duel, page_town


class DraftDuelLifecycleMixin:
    @staticmethod
    def _completion_target(con: DraftDuelConfig) -> tuple[int, bool]:
        raw = int(con.completion_count)
        if raw < 0:
            return 1, True
        return max(1, raw), False

    @staticmethod
    def _activity_open(now: datetime) -> bool:
        # 周一~周五 10:00-23:00，周六~周日 8:00-23:00
        start = 8 if now.weekday() >= 5 else 10
        return start <= now.hour < 23

    def _require_open_for_match(self):
        now = datetime.now()
        if self._activity_open(now):
            return
        target = now.replace(hour=8 if now.weekday() >= 5 else 10,
                             minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        logger.info(f'DraftDuel: closed, next attempt after {target}')
        self.set_next_run(task='DraftDuel', success=None, finish=False, target=target)
        raise TaskEnd('DraftDuel')

    def _run_leader(self, con: DraftDuelConfig):
        self.goto_page(page_draft_duel)
        logger.info('DraftDuel: lobby reached')
        logger.info(f'DraftDuel: limit_time={con.limit_time}')

        state = self._cost_state()
        if state == 'unknown':
            logger.warning('DraftDuel: unrecognized cost text, stop without entering a match')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')
        if state == 'full':
            if con.stop_when_full:
                logger.info('DraftDuel: weekly attempts full, stop_when_full -> stop')
                self.set_next_run(task='DraftDuel', success=True, finish=True)
                raise TaskEnd('DraftDuel')
            logger.info('DraftDuel: weekly attempts full, continue by paying gold')

        if not self._invite_teammate():
            logger.warning('DraftDuel: invite friend failed')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        if con.stop_when_full:
            # 勾选刷满即停：忽略完成局数，打到十把 30 万刷满为止
            target, unlimited = 1, True
            target_label = '刷满'
        else:
            target, unlimited = self._completion_target(con)
            target_label = '∞' if unlimited else target
        limit_seconds = (con.limit_time.hour * 3600 + con.limit_time.minute * 60
                         + con.limit_time.second)
        deadline = time.monotonic() + limit_seconds
        completed = 0
        if self._resume_match():
            completed = 1
        while (unlimited or completed < target) and time.monotonic() < deadline:
            if con.stop_when_full and completed > 0:
                state = self._cost_state()
                if state == 'full':
                    logger.info('DraftDuel: 10把30万已刷满, stop')
                    break
            self._reset_match_state()
            self._duel_share_reset()
            if not self._ensure_draft_lobby():
                logger.warning('DraftDuel: back to draft lobby failed')
                self.set_next_run(task='DraftDuel', success=False, finish=True)
                raise TaskEnd('DraftDuel')
            if not self._start_round():
                logger.warning('DraftDuel: start round failed')
                self.set_next_run(task='DraftDuel', success=False, finish=True)
                raise TaskEnd('DraftDuel')
            if not self._wait_battle_return():
                logger.warning('DraftDuel: battle wait failed')
                self.set_next_run(task='DraftDuel', success=False, finish=True)
                raise TaskEnd('DraftDuel')
            if not self._save_game_record() and getattr(self, '_match_record', None):
                self._match_record.save(self._run_wins, self._run_losses)
            completed += 1
            logger.info(f'DraftDuel: match {completed}/{target_label} finished')

        self.goto_page(page_town)
        self.set_next_run(task='DraftDuel', success=True, finish=True)
        raise TaskEnd('DraftDuel')

    def _run_member(self, con: DraftDuelConfig):
        # 队员不用进大厅，原地等队长的组队邀请
        logger.info('DraftDuel: wait for team invite')
        # 首次邀请只等 2 分钟；任务总时长仍由外层 limit_time 控制。
        if not self._wait_accept_invite(con, timeout=120):
            logger.warning('DraftDuel: no invite to accept')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        logger.info('DraftDuel: joined team, waiting for leader; follow leader count')
        target_label = '∞'
        limit_seconds = (con.limit_time.hour * 3600 + con.limit_time.minute * 60
                         + con.limit_time.second)
        deadline = time.monotonic() + limit_seconds
        completed = 0
        while time.monotonic() < deadline:
            self._reset_match_state()
            # 上一局结束的大厅面板，队员也点一次继续
            for _ in range(3):
                if not self._clear_final_panel():
                    break
            if completed > 0:
                if not self._wait_accept_invite(con, timeout=120):
                    logger.info('DraftDuel: leader stopped, member exits')
                    break
            if not self._wait_member_selection(con):
                if completed > 0:
                    logger.info('DraftDuel: leader stopped, member exits')
                    break
                logger.warning('DraftDuel: member selection not found')
                self.set_next_run(task='DraftDuel', success=False, finish=True)
                raise TaskEnd('DraftDuel')
            if not self._draft_phase():
                logger.warning('DraftDuel: member selection failed')
                self.set_next_run(task='DraftDuel', success=False, finish=True)
                raise TaskEnd('DraftDuel')
            logger.info(f'DraftDuel: member match {completed + 1}/{target_label}')
            self._duel_share_write(self.picked_roles)
            self._battle_mate_names = self._duel_share_read_mate()
            # 同上，暂时不打开阵容详情
            # self._read_lineup_detail()
            if not self._start_fielding() or not self._wait_battle_return():
                logger.warning('DraftDuel: member fielding or settlement failed')
                self.set_next_run(task='DraftDuel', success=False, finish=True)
                raise TaskEnd('DraftDuel')
            if not self._save_game_record() and getattr(self, '_match_record', None):
                self._match_record.save(self._run_wins, self._run_losses)
            completed += 1
        self.goto_page(page_town)
        self.set_next_run(task='DraftDuel', success=True, finish=True)
        raise TaskEnd('DraftDuel')

    def _resume_match(self) -> bool:
        """重启后如果已经在半局里，直接续接，不重新选人。"""
        self.screenshot()
        if self.appear(self.I_NEW_MATCH):
            return False
        if not (self._prep_visible()
                or self.appear(self.I_DRAFT_DUEL_COMPLETED_HALL)
                or self.appear(self.I_START)):
            return False
        names = self._duel_share_read_own()
        self.picked_roles = names or []
        self._reset_match_state()
        logger.info(f'DraftDuel: resume existing match, names={self.picked_roles}')
        self._wait_battle_return()
        return True
