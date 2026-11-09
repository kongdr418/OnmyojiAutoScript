"""倒计时推进上阵轮次、拖卡重试及锁定确认。"""

import time

from module.base.timer import Timer
from module.logger import logger
from tasks.DraftDuel.field_drag import drag_to_field
from tasks.DraftDuel.strategy import plan_duel_lineup, plan_lineup
from tasks.DraftDuel.matrix import score_team
from tasks.DraftDuel.runtime.model_policy import recommend_lineup
from tasks.DraftDuel.runtime.portrait_match import (
    HAND_PORTRAIT_SCALE, HAND_PORTRAIT_THRESHOLDS, locate_portrait,
)


class DraftDuelDeploymentMixin:
    def _locate_hand_portrait(self, face, target: str):
        """同一帧逐级降低手牌匹配阈值；每次定位从最高阈值重新开始。"""
        for threshold in HAND_PORTRAIT_THRESHOLDS:
            located = locate_portrait(
                face, self.device.image, self.HAND_AREA,
                scale=HAND_PORTRAIT_SCALE, threshold=threshold,
            )
            if located is not None:
                if threshold < HAND_PORTRAIT_THRESHOLDS[0]:
                    logger.info(
                        f'DraftDuel: {target} hand portrait matched at reduced '
                        f'threshold {threshold:.2f}, score={located[1]:.3f}'
                    )
                return located
        return None

    def _wait_slot_target(self, target: str):
        """目标连续两帧一致才通过；三次校验失败返回，由调用方重新拖卡。"""
        preview, stable, failures = None, 0, 0
        for _ in range(5):
            self.screenshot()
            self._update_field_clock()
            if self._field_epoch > self._field_round:
                return None
            actual, score = self._field_slot_actual(self._field_round)
            if actual is not None and actual == preview:
                stable += 1
            else:
                preview, stable = actual, 1 if actual else 0
            if actual != target:
                failures += 1
            if actual == target and stable >= 2:
                return actual
            if failures >= 3:
                logger.warning(
                    f'DraftDuel: target={target}, actual={actual}; '
                    'retry drag after three failed checks'
                )
                return None
            logger.info(
                f'DraftDuel: target={target}, actual={actual}, score={score:.3f}, '
                f'stable={stable}, failed_checks={failures}; wait before confirm'
            )
            self.device.sleep(0.3)
        return None

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
                if self._use_auto_form():
                    return self._auto_form_field()
                if not self._field_battle_lineup(names):
                    return False
                self._first_lineup_done = True
                return True
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
        self._field_epoch = 0
        self._field_clock = None
        self._field_clock_wall = None
        self._field_clock_candidate = None
        self._field_clock_timer = Timer(1)
        self._prep_missing_frames = 0

    def _update_field_clock(self):
        """两帧确认倒计时重置；epoch 是实际进入的上阵轮次（从 0 开始）。"""
        # 所有调用入口共用 OAS 计时器，首次立即读，随后最多每秒读一次。
        if self._field_clock_timer.started() and not self._field_clock_timer.reached():
            return
        self._field_clock_timer.reset()
        value = self.O_FIGHT_WAIT_TIME.ocr(self.device.image)
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if type(value) is not int or not 1 <= value <= 60:
            self._field_clock_candidate = None
            return
        previous = self._field_clock
        now = time.monotonic()
        expected = (max(0, previous - (now - self._field_clock_wall))
                    if previous is not None else None)
        if previous is not None and (value > previous or value - expected >= 3):
            if value - expected < 3:
                self._field_clock_candidate = None
                return
            candidate = self._field_clock_candidate
            self._field_clock_candidate = value
            if candidate is None or not 0 <= candidate - value <= 2:
                return
            self._field_epoch += 1
            logger.info(
                f'DraftDuel: countdown reset confirmed {previous} -> {value}; '
                f'deployment round={self._field_epoch + 1}'
            )
        self._field_clock_candidate = None
        self._field_clock = value
        self._field_clock_wall = now

    def _prep_visible(self, full_fallback: bool = True) -> bool:
        # 用「全部」/自动上阵图片直接判断上阵页
        return (self.appear(self.I_ALL)
                or self.appear(self.I_ENABEL_AUTO_1)
                or self.appear(self.I_CANCEL_ANTO_1))

    def _wait_field_locked(self, timeout: float = 2.5) -> str:
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            self._update_field_clock()
            if self._field_epoch > self._field_round:
                return 'reset'
            label, _ = self._prep_button_state(full_fallback=False)
            if label == 'locked':
                return 'locked'
            if label is None:
                missing = getattr(self, '_lock_missing_frames', 0) + 1
                self._lock_missing_frames = missing
                if self.appear(self.I_BATTLE_PAGE):
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
        logger.info(f'DraftDuel: round {self._field_round} actual fielded: {target or "unknown"}')
        logger.info(
            f'DraftDuel: fielding {self._field_round}/{self.FIELD_ROUNDS} locked ({why})'
        )

    def _confirm_field(self, target, why: str) -> bool:
        """一次拖动只点一次确定；没锁上就回去重拖，不再空点确定。"""
        self.screenshot()
        self._update_field_clock()
        if self._field_epoch > self._field_round:
            self._note_round_closed(None, 'countdown reset before confirm')
            return True
        label, _ = self._prep_button_state(full_fallback=False)
        if label == 'locked':
            self._note_round_closed(target, f'{why}-locked')
            return True
        if label != 'open':
            return False
        self.click(self.C_FIELD_CONFIRM, interval=1)
        outcome = self._wait_field_locked()
        if outcome in ('locked', 'battle', 'reset'):
            self._note_round_closed(target, why)
            return True
        return False

    def _field_round_once(self, target, names) -> bool:
        """一轮：拖目标卡 -> 点确定 -> 等已锁定。"""
        for drag_attempt in (1, 2, 3):
            # OCR 偶发读空：多给几帧，不要一次读空就判失败
            label = None
            for _ in range(3):
                self.screenshot()
                self._update_field_clock()
                if self._field_epoch > self._field_round:
                    return True
                label, _ = self._prep_button_state(full_fallback=False)
                if label is not None:
                    break
                self.device.sleep(0.5)
            if label != 'open':
                return label == 'locked'

            face = None
            for name, avatar in getattr(self, '_lobby_faces', None) or []:
                if name == target:
                    face = avatar
                    break
            if face is None:
                logger.warning(f'DraftDuel: target {target} has no lobby face')
                return False
            located = self._locate_hand_portrait(face, target)
            if located is None:
                logger.warning(
                    f'DraftDuel: {target} not found in hand area, '
                    f'score below {HAND_PORTRAIT_THRESHOLDS[-1]:.3f}'
                )
                continue
            (x, y, w, h), score = located
            sx, sy = x + w // 2, y + h // 2
            destinations = self.runframe(self.F_FIGHT_SHIKIGAMI_SET)
            if not destinations or len(destinations) != 6:
                logger.warning('DraftDuel: fight_shikigami_set not detected')
                continue
            own_slots = (0, 1, 2) if self._configured_role() == 'captain' else (3, 4, 5)
            area = destinations[own_slots[self._field_round]]
            dx = int(area[0] + area[2] // 2)
            dy = int(area[1] + area[3] // 2)
            # 重拖逐次上移：起点每次 10 像素，终点每次 50 像素。
            retry_offset = drag_attempt - 1
            sy = max(0, sy - 10 * retry_offset)
            dy = max(0, dy - 50 * retry_offset)
            logger.info(
                f'DraftDuel: round {self._field_round + 1} drag {target} '
                f'({sx},{sy})->({dx},{dy}) score={score:.3f} (try {drag_attempt})'
            )
            drag_to_field(self.device, (sx, sy), (dx, dy),
                          name=f'dd_field_r{self._field_round + 1}_{drag_attempt}')
            self.device.sleep(0.3)
            actual = self._wait_slot_target(target)
            if actual != target:
                if self._field_epoch > self._field_round:
                    # 倒计时已重置，交回外层推进，不能再拖或确认上一轮。
                    return True
                logger.warning(
                    f'DraftDuel: {target} not verified after drag '
                    f'{drag_attempt}/3, retry before confirming'
                )
                continue
            if self._confirm_field(actual, f'confirm{drag_attempt}'):
                return True
            logger.warning(f'DraftDuel: round {self._field_round + 1} not locked, retry')

        # 3 次都没标记/没锁上：直接点一次确定，上错一张也继续
        self.screenshot()
        self._update_field_clock()
        if self._field_epoch > self._field_round:
            return True
        label, _ = self._prep_button_state(full_fallback=False)
        actual, _ = self._field_slot_actual(self._field_round)
        if label == 'locked':
            self._note_round_closed(actual, 'system locked')
            return True
        if label == 'open':
            logger.warning(
                f'DraftDuel: {target} unmarked after 3 tries, confirm anyway'
            )
            if self._confirm_field(actual, 'confirm-anyway'):
                return True
        return False

    def _field_battle_lineup(self, names) -> bool:
        """3 轮上阵：倒计时重置放行下一轮，按钮只确认当前轮已锁定。"""
        if not names:
            return False
        if getattr(self, '_fielding_active', False):
            return False
        self._fielding_active = True
        try:
            mate = getattr(self, '_battle_mate_names', None)
            mate_plan = ()
            plan = None
            if getattr(self.config.draft_duel.draft_duel_config, 'use_trained_model', False) and mate:
                role = 'captain' if self.config.draft_duel.draft_duel_config.user_status.value == 'leader' else 'member'
                own_arg, mate_arg = (names, mate) if role == 'captain' else (mate, names)
                model_choices = recommend_lineup(own_arg, mate_arg)
                if model_choices:
                    candidate = model_choices.get(role) or []
                    if len(candidate) == 3 and all(name in names for name in candidate):
                        plan = list(candidate)
                        mate_plan = tuple(model_choices.get(
                            'member' if role == 'captain' else 'captain') or ())
                        logger.info(f'DraftDuel: model field plan own={plan} mate={mate_plan}')
            if plan is None:
                try:
                    if mate:
                        try:
                            plan, _mate_plan = plan_duel_lineup(
                                names, mate, locked=[], team_size=min(3, len(names)))
                            logger.info(f'DraftDuel: joint plan own={plan} mate={_mate_plan}')
                            mate_plan = _mate_plan
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
            breakdown = score_team((*plan, *mate_plan), expected_size=6 if mate_plan else 3,
                                   include_stats=True)
            logger.info(
                f'DraftDuel: matrix field total={breakdown.total:.1f} '
                f'{breakdown.summary()} details={breakdown.details}'
            )
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
                self._update_field_clock()
                # 已确认上一轮后，必须看到倒计时重置才允许拖下一张。
                # 若系统自动结束当前轮，也由重置补记，避免按钮漏识别卡住。
                while self._field_round < min(self._field_epoch, self.FIELD_ROUNDS):
                    self._note_round_closed(None, 'countdown reset / system selected')
                if self._field_round >= self.FIELD_ROUNDS:
                    return True
                if self._field_clock is None or self._field_epoch < self._field_round:
                    self.device.sleep(1.0)
                    continue
                label, _ = self._prep_button_state(full_fallback=False)
                if label is None:
                    missing = getattr(self, '_prep_missing_frames', 0) + 1
                    self._prep_missing_frames = missing
                    if missing >= 4:
                        # 按钮短暂漏识别不代表进入战斗，仍等倒计时/战斗标志。
                        self._field_seen_open = False
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

    def _auto_form_field(self) -> bool:
        """后续小局等待战斗；大厅未成功开启时，在上卡页补开自动上阵。"""
        timer = Timer(120).start()
        while not timer.reached():
            self.device.stuck_record_clear()
            self.screenshot()
            if self.appear(self.I_BATTLE_PAGE):
                logger.info('DraftDuel: automatic deployment entered battle')
                return True
            if not self.appear(self.I_CANCEL_ANTO_1):
                if self.appear_then_click(self.I_ENABEL_AUTO_1, interval=1):
                    logger.info('DraftDuel: 上卡页补开自动上阵，等待出现取消自动或进入战斗')
            self.device.sleep(0.5)
        logger.warning('DraftDuel: automatic deployment did not enter battle within 120s')
        return False
