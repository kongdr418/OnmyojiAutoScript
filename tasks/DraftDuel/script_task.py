# This Python file uses the following encoding: utf-8
import json
import re
import time
from datetime import datetime, timedelta

from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger

from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.DraftDuel.assets import DraftDuelAssets
from tasks.DraftDuel.config import DraftDuelConfig, UserStatus
from tasks.DraftDuel.field_drag import drag_to_field
from tasks.DraftDuel.strategy import (CATALOG, DATA_DIR, choose_pick, normalize_name,
                                      plan_duel_lineup, plan_lineup)
from tasks.DraftDuel.visual_cards import CARD_BOXES, lobby_faces, match_hand
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_draft_duel


DUEL_SHARE_DIR = DATA_DIR / 'duel_share'


class ScriptTask(GameUi, GeneralInvite, DraftDuelAssets):
    """协同对弈（Draft Duel）。"""

    AVAILABLE_AMOUNTS = ('30万', '300000')
    FULL_AMOUNTS = ('100000', '10万')
    # 选卡阶段每个账号选 5 位；上阵阶段再各上 3 位（合计 6）
    TOTAL_PICKS = 5
    FIELD_ROUNDS = 3
    AUTO_FORM_AFTER_FIRST = True
    # 队友推荐最多等到倒计时 22s（30-8）；22s 还没推荐就走策略
    RECOMMEND_HIGH = 30
    RECOMMEND_LOW = 22
    # 懒加载的 v6-small 识别模型；False 表示加载失败，回退共享 OCR
    _v6_ocr_model = None

    def run(self):
        con: DraftDuelConfig = self.config.draft_duel.draft_duel_config
        logger.hr('Draft Duel', 2)
        self._require_open_for_match()

        if con.user_status == UserStatus.MEMBER:
            self._run_member(con)
        else:
            self._run_leader(con)

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
            completed += 1
            logger.info(f'DraftDuel: match {completed}/{target_label} finished')

        self.set_next_run(task='DraftDuel', success=True, finish=True)
        raise TaskEnd('DraftDuel')

    def _run_member(self, con: DraftDuelConfig):
        # 队员不用进大厅，原地等队长的组队邀请
        logger.info('DraftDuel: wait for team invite')
        if not self._wait_accept_invite(con):
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
            self._start_fielding()
            self._wait_battle_return()
            completed += 1
        self.set_next_run(task='DraftDuel', success=True, finish=True)
        raise TaskEnd('DraftDuel')

    def _load_v6_ocr(self):
        if self._v6_ocr_model is not None:
            return self._v6_ocr_model
        try:
            from tasks.DraftDuel.v6_rec import V6Rec
            self._v6_ocr_model = V6Rec()
            logger.info('DraftDuel: v6-small rec loaded')
        except Exception as e:
            logger.warning(f'DraftDuel: v6 OCR unavailable, fallback to shared OCR: {e}')
            self._v6_ocr_model = False
        return self._v6_ocr_model

    def _ocr_text(self, rule) -> str:
        """协同用 v6-small 识别，加载失败就回退到共享的 v2 服务。"""
        model = self._load_v6_ocr()
        if model:
            x, y, w, h = rule.roi
            crop = self.device.image[y:y + h, x:x + w]
            try:
                text, score = model.recognize_single(crop)
                if score < 0.6:
                    logger.warning(
                        f'DraftDuel: v6 OCR {rule.name} low score {score:.3f}: [{text}]'
                    )
                    return ''
                return text.strip()
            except Exception as e:
                logger.warning(f'DraftDuel: v6 OCR {rule.name} failed: {e}')
        return rule.ocr(self.device.image).strip()

    def _cost_state(self) -> str:
        text = self._ocr_text(self.O_GOLD)
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

    def _ensure_draft_lobby(self) -> bool:
        """一局结束后可能已经回主界面：先导航回协同大厅。"""
        self.screenshot()
        if (self.appear(self.I_NEW_MATCH)
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
        return (self.appear(self.I_NEW_MATCH)
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

    def _draft_phase(self) -> bool:
        """选满 5 位式神：优先跟队友推荐，没有推荐时走 strategy。"""
        logger.hr('Draft selection', 2)
        picked = set()
        self.picked_roles = []
        self._team = []
        last_hint = None
        timer = Timer(300).start()
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_DRAFT_DUEL_COMPLETED_HALL):
                logger.info('DraftDuel: selection finished')
                logger.info(f'DraftDuel: lineup {self.picked_roles}')
                self._bind_lobby_faces()
                return True
            if not self.appear(self.I_SELECTION_PAGE):
                self.device.sleep(0.5)
                continue

            role_text = self._ocr_text(self.O_HINT_ROLE)
            time_text = self._ocr_text(self.O_HINT_TIME)
            index = self._parse_pick_index(role_text)
            seconds = self._parse_seconds(time_text)
            hint = (index, seconds)
            if hint != last_hint:
                logger.info(
                    f'DraftDuel: pick={index}, hint=[{role_text}] [{time_text}], {seconds}s'
                )
                last_hint = hint

            if not 1 <= index <= self.TOTAL_PICKS or index in picked:
                self.device.sleep(0.5)
                continue
            # 队友推荐在倒计时 30s~15s 之间看，到 15s 还没有再走策略
            if not 0 < seconds <= self.RECOMMEND_HIGH:
                self.device.sleep(0.5)
                continue

            slot = self._recommended_slot()
            if slot is None and seconds <= self.RECOMMEND_LOW:
                slot = self._choose_by_strategy()
            if slot is None:
                if seconds > self.RECOMMEND_LOW:
                    # 推荐可能晚一点才出现，继续等
                    self.device.sleep(0.5)
                    continue
                # 到 15s 还没有推荐和策略：不退出，等系统倒计时结束自动选，
                # 否则任务提前结束，调度器会把队伍带走
                logger.warning(f'DraftDuel: pick {index} wait auto select')
                if self._wait_pick_change(index, timeout=seconds + 2):
                    self.picked_roles.append('')
                    picked.add(index)
                    logger.info(f'DraftDuel: pick {index} auto selected')
                    continue
                logger.warning('DraftDuel: pick did not advance after countdown')
                return False

            raw_name = self._role_name(slot)
            name = normalize_name(raw_name) or raw_name
            if name != raw_name:
                logger.info(f'DraftDuel: normalize name {raw_name!r} -> {name!r}')
            self.click(self._choose_slots()[slot], interval=1)
            if not self._wait_pick_change(index):
                logger.warning('DraftDuel: pick did not advance, retry')
                self.device.sleep(1)
                continue
            self.picked_roles.append(name)
            self._team.append(name)
            logger.info(f'DraftDuel: pick {index} slot {slot + 1} -> {name}')
            picked.add(index)
            self.device.sleep(1)
        logger.warning('DraftDuel: selection timeout')
        return False

    def _bind_lobby_faces(self):
        """选完后把大厅顶部头像按顺序和 picked_roles 绑定。"""
        faces = lobby_faces(self.device.image)
        if len(faces) != len(self.picked_roles):
            logger.warning(
                f'DraftDuel: lobby faces {len(faces)} != picks {len(self.picked_roles)}'
            )
            return
        self._lobby_faces = list(zip(self.picked_roles, faces))
        logger.info(f'DraftDuel: lobby faces bound to {self.picked_roles}')

    def _match_hand_cards(self):
        """上阵界面手牌卡位 -> 式神名；只截图，不点卡。"""
        pairs = getattr(self, '_lobby_faces', None)
        if not pairs:
            logger.warning('DraftDuel: no lobby faces bound, skip hand matching')
            return {}
        result = {}
        for slot, index, score in match_hand(self.device.image, [f for _, f in pairs]):
            name = pairs[index][0]
            result[slot] = (name, score)
            tag = ' (elimination)' if score < 0.6 else ''
            logger.info(f'DraftDuel: hand card {slot + 1} -> {name}, score={score:.3f}{tag}')
        return result

    def _card_marked(self, slot: int) -> bool:
        """只看目标卡位有没有上阵标记，避免被前面已上阵的卡误导。"""
        box = CARD_BOXES[slot]
        old_roi = self.I_MARK.roi_back
        try:
            matches = self.I_MARK.match_all(
                self.device.image, threshold=self.I_MARK.threshold,
                roi=list(box), frame_id=self.device.image_frame_id,
            )
        finally:
            self.I_MARK.roi_back = old_roi
        return bool(matches)

    def _screen_text(self) -> str:
        try:
            results = self.O_SCREEN.detect_and_ocr(self.device.image)
        except Exception as e:
            logger.warning(f'DraftDuel: screen OCR failed: {e}')
            return ''
        return ''.join(str(item.ocr_text or '') for item in results)

    def _read_lineup_detail(self):
        """选后大厅打开「阵容详情」，OCR 5 个名字并和 picked_roles 对照。"""
        self.screenshot()
        opened = False
        for card in (self.C_DETAIL_CARD_1, self.C_DETAIL_CARD_2, self.C_DETAIL_CARD_3,
                     self.C_DETAIL_CARD_4, self.C_DETAIL_CARD_5):
            self.click(card, interval=0.4)
            self.device.sleep(0.4)
            self.screenshot()
            title = str(self.O_DETAIL_TITLE.ocr(self.device.image) or '')
            if '阵容详情' in title or '阵容详情' in self._screen_text():
                opened = True
                break
        if not opened:
            logger.warning('DraftDuel: lineup detail not opened')
            return None

        names = []
        try:
            self.click(self.C_MINE_TAB, interval=0.3)
            self.device.sleep(0.4)
            for _ in range(3):
                self.screenshot()
                names = []
                for rule in (self.O_LINEUP_NAME_1, self.O_LINEUP_NAME_2,
                             self.O_LINEUP_NAME_3, self.O_LINEUP_NAME_4,
                             self.O_LINEUP_NAME_5):
                    raw = str(rule.ocr(self.device.image) or '')
                    names.append(normalize_name(raw) or raw)
                if all(names):
                    break
                self.device.sleep(0.3)
            logger.info(f'DraftDuel: lineup detail names {names}')
            if all(names) and set(names) != set(self.picked_roles):
                logger.warning(
                    f'DraftDuel: lineup detail differs from picks: {names} vs {self.picked_roles}'
                )
            self._battle_detail = tuple(names) if all(names) else None
            return self._battle_detail
        finally:
            self.click(self.C_DETAIL_CLOSE, interval=0.2)

    def _prep_button_state(self, full_fallback: bool = True):
        """上阵界面右下角：确定/已锁定 + 自动上阵状态，全部用图片判断。"""
        auto_on = self.appear(self.I_CANCEL_ANTO_1) or self.appear(self.I_CANCEL_AUTO_2)
        if self.appear(self.I_LOCKED):
            return 'locked', auto_on
        if self.appear(self.I_CONFIRM):
            return 'open', auto_on
        return None, auto_on

    def _start_fielding(self) -> bool:
        """选后大厅点「战」进上阵界面，然后上 3 张。"""
        self.screenshot()
        if self._configured_role() == 'captain' and self.appear(self.I_START):
            self.appear_then_click(self.I_START, interval=1)
        else:
            logger.info('DraftDuel: no start button, wait for leader/prep')
        timer = Timer(60).start()
        while not timer.reached():
            self.screenshot()
            label, _ = self._prep_button_state()
            if label in ('open', 'locked'):
                logger.info(f'DraftDuel: prep screen reached, state={label}')
                names = getattr(self, '_battle_detail', None) or self.picked_roles
                self._battle_fielded = True
                return self._field_battle_lineup(names)
            self.device.sleep(0.5)
        logger.warning('DraftDuel: prep screen not found')
        return False

    def _begin_new_battle_field(self):
        """每场战斗的上阵界面都是空的，单场状态在这里归零。"""
        self._field_round = 0
        self._field_seen_open = False
        self._field_confirms = []
        self._battle_locked = []
        self._lock_missing_frames = 0

    def _prep_visible(self, full_fallback: bool = True) -> bool:
        label, _ = self._prep_button_state(full_fallback)
        if label is None:
            return False
        # 结算页也有“确定”，必须同时看到自动上阵/取消自动按钮才算上阵页
        return (self.appear(self.I_ENABEL_AUTO_1)
                or self.appear(self.I_CANCEL_ANTO_1))

    def _wait_field_locked(self, timeout: float = 2.5) -> str:
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            label, _ = self._prep_button_state(full_fallback=False)
            if label == 'locked':
                return 'locked'
            if label is None:
                missing = getattr(self, '_lock_missing_frames', 0) + 1
                self._lock_missing_frames = missing
                if missing >= 2 and not self._prep_visible(full_fallback=False):
                    return 'battle'
            else:
                self._lock_missing_frames = 0
            self.device.sleep(1.0)
        return 'open'

    def _note_round_closed(self, target, why):
        self._field_round = getattr(self, '_field_round', 0) + 1
        self._field_seen_open = False
        if target:
            self._battle_locked = (getattr(self, '_battle_locked', []) or []) + [target]
        logger.info(
            f'DraftDuel: fielding {self._field_round}/{self.FIELD_ROUNDS} locked ({why})'
        )

    def _field_round_once(self, target, names) -> bool:
        """一轮：拖目标卡 -> 点确定 -> 等已锁定。"""
        for drag_attempt in (1, 2, 3):
            # OCR 偶发读空：多给几帧，不要一次读空就判失败
            label = None
            for _ in range(3):
                self.screenshot()
                label, _ = self._prep_button_state(full_fallback=False)
                if label is not None:
                    break
                self.device.sleep(0.5)
            if label != 'open':
                return label == 'locked'

            mapping = self._match_hand_cards()
            inverse = {name: slot for slot, (name, _score) in mapping.items()}
            slot = inverse.get(target)
            if slot is None:
                # OCR 可能漏字：因幡辉夜姬 -> 辉夜姬；只在唯一包含关系时采用
                near = [s for s, n in mapping.items()
                        if n and (n in target or target in n)]
                if len(near) == 1:
                    slot = near[0]
                    logger.info(
                        f'DraftDuel: fuzzy target {target} -> {mapping[slot]}'
                    )
            if slot is None:
                logger.warning(f'DraftDuel: target {target} not in hand {mapping}')
                return False
            swipe = getattr(self, f'S_FIELD_DRAG_{slot + 1}_{self._field_round + 1}')
            x1, y1, x2, y2 = swipe.coord()
            logger.info(
                f'DraftDuel: round {self._field_round + 1} drag {target} '
                f'card{slot + 1} {(x1, y1)}->{(x2, y2)} (try {drag_attempt})'
            )
            drag_to_field(self.device, (x1, y1), (x2, y2),
                          name=f'dd_field_r{self._field_round + 1}_{drag_attempt}')
            self.device.sleep(4)
            self.screenshot()
            if not self._card_marked(slot):
                logger.warning(
                    f'DraftDuel: {target} card{slot + 1} not marked after drag, retry'
                )
                continue
            self.click(self.C_FIELD_CONFIRM, interval=1)
            outcome = self._wait_field_locked()
            if outcome in ('locked', 'battle'):
                self._note_round_closed(target, f'confirm{drag_attempt}')
                return True
            logger.warning(f'DraftDuel: round {self._field_round + 1} not locked, retry')

        # 3 次都没标记/没锁上：直接点一次确定，上错一张也继续
        self.screenshot()
        label, _ = self._prep_button_state(full_fallback=False)
        if label == 'locked':
            self._note_round_closed(target, 'system locked')
            return True
        if label == 'open':
            logger.warning(
                f'DraftDuel: {target} unmarked after 3 tries, confirm anyway'
            )
            self.click(self.C_FIELD_CONFIRM, interval=1)
            outcome = self._wait_field_locked()
            if outcome in ('locked', 'battle'):
                self._note_round_closed(target, 'confirm-anyway')
                return True
        return False

    def _field_battle_lineup(self, names) -> bool:
        """3 轮上阵：以「确定/已锁定」为准推进轮次，不按 loop 下标硬推。"""
        if not names:
            return False
        if getattr(self, '_fielding_active', False):
            return False
        self._fielding_active = True
        try:
            mate = getattr(self, '_battle_mate_names', None)
            try:
                if mate:
                    try:
                        plan, _mate_plan = plan_duel_lineup(
                            names, mate, locked=[], team_size=min(3, len(names)))
                        logger.info(f'DraftDuel: joint plan own={plan} mate={_mate_plan}')
                    except Exception as e:
                        logger.warning(f'DraftDuel: joint plan failed, fallback: {e}')
                        plan = plan_lineup(names, team_size=min(3, len(names)),
                                           locked=[], max_fire=1)
                else:
                    plan = plan_lineup(names, team_size=min(3, len(names)), locked=[],
                                       max_fire=1)
            except Exception as e:
                logger.warning(f'DraftDuel: field plan failed: {e}')
                return False
            logger.info(f'DraftDuel: field plan {plan}')
            for _i, _name in enumerate(plan, 1):
                logger.info(f'DraftDuel: round {_i} will field {_name}')
            self._begin_new_battle_field()
            timer = Timer(150).start()
            while self._field_round < self.FIELD_ROUNDS:
                if timer.reached():
                    logger.warning('DraftDuel: fielding timeout')
                    return False
                if self._field_round >= len(plan):
                    return True
                self.screenshot()
                if self.appear(self.I_BATTLE_PAGE):
                    logger.info('DraftDuel: battle page detected, stop fielding')
                    return True
                label, _ = self._prep_button_state(full_fallback=False)
                if label is None:
                    missing = getattr(self, '_prep_missing_frames', 0) + 1
                    self._prep_missing_frames = missing
                    if missing >= 4:
                        logger.info(
                            f'DraftDuel: prep gone, battle started at '
                            f'{self._field_round}/3'
                        )
                        return True
                    self.device.sleep(1.0)
                    continue
                self._prep_missing_frames = 0
                if label == 'locked':
                    if self._field_seen_open:
                        self._note_round_closed(None, 'system locked')
                    self.device.sleep(1.0)
                    continue
                self._field_seen_open = True
                target = plan[self._field_round]
                if not self._field_round_once(target, names):
                    logger.warning(f'DraftDuel: round {self._field_round + 1} failed')
                    return False
                self.device.sleep(0.3)
            logger.info('DraftDuel: fielding done 3/3')
            return True
        finally:
            self._fielding_active = False

    def _reset_match_state(self):
        self._run_wins = 0
        self._run_losses = 0
        self._result_counted = False
        self._battle_fielded = False
        self._battle_count = 0
        self._battle_mate_names = None
        self._reward_seen = False
        self._match_ended = False

    def _configured_role(self) -> str:
        con = self.config.draft_duel.draft_duel_config
        return 'member' if con.user_status == UserStatus.MEMBER else 'captain'

    def _duel_share_reset(self):
        for role in ('captain', 'member'):
            path = DUEL_SHARE_DIR / f'draft_share_{role}.json'
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

    def _auto_form_field(self) -> bool:
        """上阵页：用图片判断自动上阵状态，不再整屏 OCR。

        上阵页的按钮在 (939,587)，大厅页在 (995,617)；
        这里只在上阵页调用，所以用 I_ENABEL_AUTO_1 / I_CANCEL_ANTO_1。
        """
        self.screenshot()
        if self.appear(self.I_CANCEL_ANTO_1):
            self._click_rule(self.C_FIELD_CONFIRM, 'dd_field_confirm')
            self.device.sleep(1.0)
            return True
        if self.appear(self.I_ENABEL_AUTO_1):
            self._click_rule(self.I_ENABEL_AUTO_1, 'dd_auto_form')
            self.device.sleep(0.8)
            self._click_rule(self.C_FIELD_CONFIRM, 'dd_field_confirm')
            self.device.sleep(1.0)
            return True
        return False

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
                            if not self._field_battle_lineup(names):
                                logger.warning('DraftDuel: fielding failed')
                                return False
                    elif not self._field_battle_lineup(names):
                        logger.warning('DraftDuel: fielding failed')
                        return False
                self.device.sleep(1.0)
                continue
            self._battle_fielded = False

            if self._configured_role() == 'captain' and self.appear(self.I_START):
                self._result_counted = False
                self._battle_fielded = False
                self.appear_then_click(self.I_START, interval=1)
                continue
            self.device.sleep(0.5)
        logger.warning('DraftDuel: battle timeout')
        return self._run_finished()

    def _wait_pick_change(self, index: int, timeout: float = 3) -> bool:
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            text = self._ocr_text(self.O_HINT_ROLE)
            if self._parse_pick_index(text) != index:
                return True
            self.device.sleep(0.5)
        return False

    def _choose_slots(self):
        return (self.C_CHOOSE_1, self.C_CHOOSE_2, self.C_CHOOSE_3)

    def _role_name(self, slot: int) -> str:
        """读三张卡里对应位置的名字。"""
        rules = (self.O_ROLE_1, self.O_ROLE_2, self.O_ROLE_3)
        name = self._ocr_text(rules[slot])
        if not name:
            logger.warning(f'DraftDuel: role {slot + 1} name OCR empty')
        return name

    def _recommended_slot(self):
        matches = self.I_RECOMMEND_ROLE.match_all_any(
            self.device.image, frame_id=self.device.image_frame_id
        )
        if not matches:
            return None
        # 匹配结果是 (score, x, y, w, h)，按 x 中心归到左中右三张卡
        slot_centers = (273, 648, 1023)
        for _, x, y, w, h in matches:
            center = x + w / 2
            slot = min(range(3), key=lambda i: abs(center - slot_centers[i]))
            if abs(center - slot_centers[slot]) <= 150:
                return slot
        return None

    def _choose_by_strategy(self):
        """队友没有推荐时用 strategy.choose_pick，返回卡槽下标 0~2。"""
        raw = [self._role_name(slot) for slot in range(3)]
        names = [normalize_name(name) for name in raw]
        offers = [name for name in names if name]
        if not offers:
            logger.warning(f'DraftDuel: strategy no offers, raw={raw}')
            return None
        try:
            # OCR 可能把未归一化的名字写进队伍，过滤掉图鉴里没有的，避免 KeyError
            team = [item for item in self._team if item in CATALOG]
            decision = choose_pick(offers, team, team_size=self.TOTAL_PICKS)
        except Exception as e:
            logger.warning(f'DraftDuel: strategy failed: {e}')
            return None
        try:
            slot = names.index(decision.name)
        except ValueError:
            logger.warning(f'DraftDuel: strategy pick {decision.name} not in {names}')
            return None
        logger.info(
            f'DraftDuel: strategy {decision.name} score={decision.score:.1f} '
            f'reasons={decision.reasons} souls={decision.suggested_souls}'
        )
        return slot

    @staticmethod
    def _parse_pick_index(text: str) -> int:
        match = re.search(r'第\s*(\d+)', text or '')
        if match:
            return int(match.group(1))
        nums = re.findall(r'\d+', text or '')
        return int(nums[0]) if nums else 0

    @staticmethod
    def _parse_seconds(text: str) -> int:
        # 倒计时最多 30 秒
        match = re.search(r'(\d{1,2})\s*[sS]', text or '')
        if match:
            return min(30, int(match.group(1)))
        digits = re.sub(r'\D', '', text or '')
        if not digits:
            return 0
        return min(30, int(digits[:2]))

    def _wait_accept_invite(self, con: DraftDuelConfig, timeout: float | None = None) -> bool:
        # 队员等邀请默认用 limit_time；第二局起只等 120 秒，队长不打就退出
        wait_seconds = (timeout if timeout is not None
                        else con.limit_time.hour * 3600 + con.limit_time.minute * 60
                        + con.limit_time.second)
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
