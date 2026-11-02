"""同机双方名单交换及本端自动上阵检查。"""

import json
import time

from module.logger import logger
from tasks.DraftDuel.config import UserStatus
from tasks.DraftDuel.strategy import DATA_DIR, normalize_name


DUEL_SHARE_DIR = DATA_DIR / 'duel_share'


class DraftDuelCooperationMixin:
    def _reset_match_state(self):
        self._run_wins = 0
        self._run_losses = 0
        self._result_counted = False
        self._battle_fielded = False
        self._battle_count = 0
        self._battle_mate_names = None
        self._reward_seen = False
        self._match_ended = False
        self._auto_wait_timer = None
        self._auto_wait_reason = None
        self._first_battle_waited = False

    def _configured_role(self) -> str:
        con = self.config.draft_duel.draft_duel_config
        return 'member' if con.user_status == UserStatus.MEMBER else 'captain'

    def _duel_share_reset(self):
        for role in ('captain', 'member'):
            for prefix in ('draft_share', 'auto_ready'):
                path = DUEL_SHARE_DIR / f'{prefix}_{role}.json'
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except Exception as e:
                    logger.warning(f'DraftDuel: share reset failed: {e}')

    def _duel_share_write(self, names):
        if not names:
            return
        role = self._configured_role()
        try:
            DUEL_SHARE_DIR.mkdir(parents=True, exist_ok=True)
            payload = {'role': role, 'names': list(names),
                       'ts': time.time(), 'state': 'ready'}
            (DUEL_SHARE_DIR / f'draft_share_{role}.json').write_text(
                json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            logger.info(f'DraftDuel: share written ({role}): {list(names)}')
        except Exception as e:
            logger.warning(f'DraftDuel: share write failed: {e}')

    def _duel_share_read(self, role: str, max_age: float = 600):
        try:
            data = json.loads((DUEL_SHARE_DIR / f'draft_share_{role}.json')
                              .read_text(encoding='utf-8'))
        except Exception:
            return None
        if data.get('state') != 'ready':
            return None
        names = [normalize_name(raw) for raw in data.get('names', [])]
        names = [name for name in names if name]
        if len(names) < 3 or time.time() - float(data.get('ts') or 0) > max_age:
            return None
        return names

    def _duel_share_read_own(self):
        return self._duel_share_read(self._configured_role())

    def _duel_share_read_mate(self, timeout: float = 6.0):
        role = 'member' if self._configured_role() == 'captain' else 'captain'
        deadline = time.time() + timeout
        while time.time() < deadline:
            names = self._duel_share_read(role, max_age=300)
            if names:
                logger.info(f'DraftDuel: mate share read ({role}): {names}')
                return names
            time.sleep(0.5)
        return None

    def _ensure_lobby_auto_form(self) -> bool:
        """后续小局只确认本端 auto_right，不依赖队友 OAS 就绪文件。"""
        if getattr(self, '_battle_count', 0) < 1:
            return True
        if not any(self.appear(rule) for rule in (
                self.I_START, self.I_DRAFT_DUEL_COMPLETED_HALL, self.I_MEMBERS_AWAIT)):
            return False
        ready = bool(self.appear(self.I_AUTO_RIGHT))
        if not ready and self.appear(self.I_AUTO_DOWN):
            logger.info('DraftDuel: enable lobby automatic deployment; wait for auto_right')
            self.appear_then_click(self.I_AUTO_DOWN, interval=1)
        reason = None if ready else '等待本端出现 auto_right'
        if reason != self._auto_wait_reason:
            logger.info(f'DraftDuel: {reason or "本端自动上阵已确认，允许匹配"}')
            self._auto_wait_reason = reason
        return ready
