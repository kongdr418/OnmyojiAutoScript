"""同机双方名单交换及本端自动上阵检查。"""

import json
import time

from module.logger import logger
from tasks.DraftDuel.config import UserStatus
from tasks.DraftDuel.runtime.game_record import GameRecordReader, save_run
from tasks.DraftDuel.runtime.match_record import RealMatchRecorder
from tasks.DraftDuel.strategy import DATA_DIR, normalize_name


DUEL_SHARE_DIR = DATA_DIR / 'duel_share'


class DraftDuelCooperationMixin:
    def _reset_match_state(self):
        self._run_started_at = time.time()
        self._match_record = None
        try:
            con = self.config.draft_duel.draft_duel_config
            if getattr(con, 'save_real_match', False):
                self._match_record = RealMatchRecorder(self._configured_role())
        except Exception as e:
            logger.warning(f'DraftDuel: real match recorder init failed: {e}')
        self._run_wins = 0
        self._run_losses = 0
        self._result_counted = False
        self._battle_fielded = False
        self._battle_count = 0
        self._first_lineup_done = False
        self._battle_mate_names = None
        self._reward_seen = False
        self._match_ended = False
        self._auto_wait_timer = None
        self._auto_wait_reason = None
        self._first_battle_waited = False

    def _configured_role(self) -> str:
        con = self.config.draft_duel.draft_duel_config
        return 'member' if con.user_status == UserStatus.MEMBER else 'captain'

    def _save_game_record(self) -> bool:
        """Prefer the game's own pvp2v2_record, matched by run time."""
        con = self.config.draft_duel.draft_duel_config
        if not getattr(con, 'save_real_match', False):
            return False
        try:
            reader = GameRecordReader(self.device)
            found = reader.latest_run(getattr(self, '_run_started_at', time.time() - 600))
            if not found:
                logger.warning('DraftDuel: no matching game pvp2v2_record found')
                return False
            draft = getattr(getattr(self, '_match_record', None), 'data', {}).get('draft')
            save_run(found, self._configured_role(), self._run_started_at, draft=draft)
            return True
        except Exception as exc:
            logger.warning(f'DraftDuel: game record match failed: {exc}')
            return False

    def _duel_share_reset(self):
        for role in ('captain', 'member'):
            for prefix in ('draft_share', 'draft_progress', 'auto_ready'):
                path = DUEL_SHARE_DIR / f'{prefix}_{role}.json'
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except Exception as e:
                    logger.warning(f'DraftDuel: share reset failed: {e}')

    def _draft_share_write(self, names, offers=(), round_number=None):
        """选卡阶段单独交换进度，不把未选满的名单标为可上场。"""
        role = self._configured_role()
        try:
            DUEL_SHARE_DIR.mkdir(parents=True, exist_ok=True)
            path = DUEL_SHARE_DIR / f'draft_progress_{role}.json'
            payload = {'names': list(names), 'offers': list(offers),
                       'round': round_number, 'started': self._draft_started_at,
                       'ts': time.time()}
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
        except Exception as e:
            logger.warning(f'DraftDuel: draft progress write failed: {e}')

    def _draft_share_read_mate(self):
        return self._draft_share_read_snapshot().get('names', [])

    def _draft_share_read_snapshot(self):
        """只读本次进入选卡后更新的队友进度；缺失或过期时按单方评分。"""
        role = 'member' if self._configured_role() == 'captain' else 'captain'
        try:
            path = DUEL_SHARE_DIR / f'draft_progress_{role}.json'
            payload = json.loads(path.read_text(encoding='utf-8'))
            timestamp = float(payload['ts'])
            # 不以对方写入时间必须晚于本端入场为条件：双方入场存在时差。
            started = float(payload.get('started', timestamp))
            if abs(started - self._draft_started_at) > 15 or timestamp > time.time():
                return {}
            if time.time() - timestamp > 180:
                return {}
            if not isinstance(payload['names'], list) or not isinstance(payload.get('offers', []), list):
                return {}
            names = [normalize_name(raw) for raw in payload['names']]
            offers = [normalize_name(raw) for raw in payload.get('offers', [])]
            return {'names': list(dict.fromkeys(name for name in names if name))[:5],
                    'offers': offers if len(offers) == 3 and all(offers) and len(set(offers)) == 3 else [],
                    'round': payload.get('round')}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {}

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

    def _use_auto_form(self) -> bool:
        """首局手动上阵完成后即可自动；不依赖胜负页面是否识别成功。"""
        return self.AUTO_FORM_AFTER_FIRST and (
            getattr(self, '_first_lineup_done', False) or getattr(self, '_battle_count', 0) >= 1
        )

    def _is_new_round_lobby(self) -> bool:
        """固定大厅标题加未开启标志，区别于保留五张头像的小局待匹配页。"""
        return self.appear(self.I_LOBBY_TITLE) and (
            self.appear(self.I_NEW_MATCH) or self.appear(self.I_WAIT_FIGHT_LOBBY)
        )

    def _ensure_lobby_auto_form(self) -> bool:
        """后续小局只确认本端 auto_right，不依赖队友 OAS 就绪文件。"""
        if not self._use_auto_form():
            return True
        if self._is_new_round_lobby():
            return False
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
