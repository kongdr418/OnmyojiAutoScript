"""大富翁独有棋盘流程。"""

import random
import time

from module.exception import GameStuckError
from module.logger import logger
from tasks.ActivityShikigami.base_act import ActivityResourceNotEnough
import tasks.ActivityShikigami.page as pages


RICHMAN_ENTRY_SETTLE_SECONDS = 1.0


class RichManAct:
    def setup_rich_man_pages(self):
        page_act = self.navigator.resolve_page(pages.page_act)
        page_board = self.navigator.resolve_page(pages.page_rich_man)

        def enter_board(task) -> bool:
            logger.info(f'Wait {RICHMAN_ENTRY_SETTLE_SECONDS:.1f}s for RichMan activity overlays')
            time.sleep(RICHMAN_ENTRY_SETTLE_SECONDS)
            if not pages.handle_activity_overlay(task):
                return False
            task.screenshot()
            return task.appear_then_click(task.I_RM_TO_BATTLE_MAIN, interval=0)

        page_act.connect(page_board, enter_board, key='activity->rich_man')

    def run_rich_man(self):
        logger.hr('Start activity: RichMan', 1)
        self.setup_rich_man_pages()
        self.switch_soul_for_from_courtyard('rich_man')
        self.goto_page(pages.page_rich_man)
        common_loadout_required = True
        self._boss_pending = False
        self._boss_wait_exp_reset = False
        self._boss_max_level = False
        self._last_rich_man_throw_count = None

        try:
            while True:
                self.screenshot()
                if self.ui_reward_appear_click():
                    continue
                if self.appear_then_click(self.I_UI_CONFIRM, interval=2):
                    continue

                if not self.appear(self.I_RM_THROW):
                    time.sleep(0.3)
                    continue
                if self._sync_rich_man_team_lock(switch_loadout=common_loadout_required):
                    continue

                if self._boss_should_enter():
                    self._run_rich_man_boss_fight(switch_loadout=common_loadout_required)
                    self._wait_for_stable_throw_before_next_round()
                    common_loadout_required = False
                    self._boss_pending = False
                    self._boss_wait_exp_reset = True
                    continue

                # 全局时间与随机休眠都只在真正开始下一次掷骰子前生效。
                if not self.prepare_next_action('rich_man'):
                    return

                raw_dice_count = self.O_CINQUE_COUNT.ocr_digit(self.device.image)
                dice_count = self._normalize_rich_man_dice_count(raw_dice_count)
                logger.info(f'RichMan dice count before throw: {dice_count}')

                logger.hr('Throw ticket', 3)
                self.device.stuck_record_clear()
                self.device.stuck_record_add('BATTLE_STATUS_S')
                mode = self._throw_until_dice_count_changes(dice_count)
                self.record_action('rich_man')
                if mode == 'throw':
                    self._run_throw_task()
                elif mode == 'rob':
                    self._run_rob_task()
                elif mode == 'fight':
                    self._run_rich_man_fight(switch_loadout=common_loadout_required)
                    common_loadout_required = False
                if mode is not None:
                    self._wait_for_stable_throw_before_next_round()
        except ActivityResourceNotEnough:
            logger.info('RichMan dice exhausted')

    def _normalize_rich_man_dice_count(self, raw_count: int) -> int:
        """结合上一次成功投掷前的数量，修正本轮为零的 OCR 结果。"""
        if raw_count > 0:
            return raw_count

        previous_count = self._last_rich_man_throw_count
        if previous_count is None:
            logger.info('RichMan dice count is 0 on entry; stop throwing')
            raise ActivityResourceNotEnough
        if previous_count <= 1:
            logger.info(
                f'RichMan previous throw count was {previous_count}, '
                'current count is 0; dice exhausted'
            )
            raise ActivityResourceNotEnough

        corrected_count = previous_count - 1
        logger.warning(
            f'RichMan dice OCR returned 0 after previous throw count '
            f'{previous_count}; correct current count to {corrected_count}'
        )
        return corrected_count

    def _read_level_experience(self) -> tuple[int, int] | None:
        current, _, total = self.O_LEVEL_EXPERIENCE.ocr_digit_counter(self.device.image)
        if total <= 0:
            logger.warning(f'Invalid RichMan level experience OCR: {current}/{total}')
            return None
        logger.info(f'RichMan level experience: {current}/{total}')
        return current, total

    def _boss_level_is_max(self) -> bool:
        if self._boss_max_level:
            return True
        current, _, total = self.O_LEVEL.ocr_digit_counter(self.device.image)
        logger.info(f'RichMan level: {current}/{total}')
        if current == 10 and total == 10:
            logger.info('RichMan max level reached, disable subsequent boss challenges')
            self._boss_max_level = True
            self._boss_pending = False
            self._boss_wait_exp_reset = False
            return True
        return False

    def _confirm_boss_experience_overflow(self) -> bool:
        first = self._read_level_experience()
        if first is None or first[0] <= first[1]:
            return False
        time.sleep(0.3)
        self.screenshot()
        second = self._read_level_experience()
        if second != first:
            logger.warning(f'RichMan boss experience confirmation mismatch: {first} -> {second}')
            return False
        logger.info(f'RichMan boss challenge pending at experience {first[0]}/{first[1]}')
        return True

    def _boss_anchor_appear(self) -> bool:
        return self.appear(self.I_RM_FITGHT_ANCHOR)

    def _boss_should_enter(self) -> bool:
        if self._boss_level_is_max():
            return False
        if self._boss_wait_exp_reset:
            experience = self._read_level_experience()
            if experience is not None and experience[0] <= experience[1]:
                logger.info('RichMan boss experience reset confirmed')
                self._boss_wait_exp_reset = False
            return False
        if not self._boss_pending:
            self._boss_pending = self._confirm_boss_experience_overflow()
        if not self._boss_pending:
            return False
        if self._boss_anchor_appear():
            logger.info(f'RichMan boss anchor found at {self.I_RM_FITGHT_ANCHOR.roi_front}')
            return True
        logger.info('RichMan boss pending, anchor is outside its roi_back or not visible')
        return False

    def _throw_until_dice_count_changes(
            self, previous_count: int
    ) -> str | None:
        """点击投掷直到数字变化，再限时识别投掷后的三种分支。"""
        attempt = 0
        while True:
            self.screenshot()
            if self.appear(self.I_CINQUE_NOT_ENOUGH):
                logger.warning('RichMan dice not enough prompt appeared')
                self.click(self.C_RM_RANDOM_CLOSE_SAFE_MAIN, interval=1)
                self.device.click_record_clear()
                raise ActivityResourceNotEnough

            current_count = self.O_CINQUE_COUNT.ocr_digit(self.device.image)
            if current_count != previous_count:
                logger.info(
                    f'RichMan dice count changed: '
                    f'{previous_count} -> {current_count}; throw succeeded'
                )
                self._last_rich_man_throw_count = previous_count
                self.device.click_record_clear()
                mode_timeout = 8.0
                logger.info(
                    f'Wait up to {mode_timeout:.2f}s for RichMan mode'
                )
                return self._wait_for_mode_after_throw(
                    timeout=mode_timeout
                )

            if self.appear_then_click(self.I_RM_THROW, interval=2):
                attempt += 1
                logger.info(
                    f'RichMan dice count unchanged at {previous_count}; '
                    f'click throw: attempt={attempt}'
                )
            time.sleep(0.3)

    def _detect_richman_mode(self) -> str | None:
        if self.appear(self.I_RM_MODE_THROW):
            return 'throw'
        if self.appear(self.I_RM_MODE_ROB):
            return 'rob'
        if self.appear(self.I_RM_MODE_FIGHT):
            return 'fight'
        return None

    def _wait_for_mode_after_throw(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.screenshot()
            mode = self._detect_richman_mode()
            if mode is not None:
                logger.info(f'RichMan mode detected after throw: {mode}')
                return mode
            time.sleep(0.2)
        logger.info(
            f'RichMan mode not detected within {timeout:.1f}s; '
            'possibly reward tile, continue to next throw cycle'
        )
        return None

    def _run_throw_task(self):
        logger.hr('RichMan throw task', 3)
        deadline = time.monotonic() + 20.0
        attempt = 0
        while True:
            if time.monotonic() >= deadline:
                raise GameStuckError('RichMan throw mode timed out after 20 seconds')
            self.screenshot()
            if self.appear(self.I_RM_THROW):
                logger.info(f'RichMan throw task finished after {attempt} attempt(s)')
                return
            if self.appear(self.I_RM_MODE_THROW) and \
                    self.appear_then_click(self.I_RM_THROW_FIGHT, interval=1):
                attempt += 1
                continue
            if self.ui_reward_appear_click():
                continue
            if self.appear_then_click(self.I_UI_CONFIRM, interval=1) or \
                    self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1):
                continue
            time.sleep(0.5)

    def _run_rob_task(self):
        logger.hr('RichMan rob task', 3)
        choices = [
            self.C_RM_ROB_CHOICE_1,
            self.C_RM_ROB_CHOICE_2,
            self.C_RM_ROB_CHOICE_3,
            self.C_RM_ROB_CHOICE_4,
        ]
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            choice = random.choice(choices)
            logger.info(
                f'RichMan rob choice: {choice.name}, '
                f'attempt={attempt}/{max_attempts}'
            )
            self.click(choice)

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                self.screenshot()
                if self.appear(self.I_RM_THROW):
                    logger.info(
                        f'RichMan rob task finished: '
                        f'attempt={attempt}/{max_attempts}'
                    )
                    return
                time.sleep(0.3)

            self.screenshot()
            if self.appear(self.I_RM_MODE_ROB):
                if attempt < max_attempts:
                    logger.warning(
                        'RichMan rob choice timed out and rob mode is '
                        f'still active; retry attempt={attempt + 1}/'
                        f'{max_attempts}'
                    )
                    continue
                raise GameStuckError(
                    'RichMan rob mode remained active after 3 attempts'
                )

            raise GameStuckError(
                'RichMan rob mode timed out before returning to throw main'
            )

    def _run_rich_man_fight(self, switch_loadout: bool):
        logger.hr('RichMan fight task', 3)
        source = self.battle_config('rich_man')
        battle = source.copy(update={
            'lock_team_enable': False,
            'preset_enable': source.preset_enable and switch_loadout,
            'continuous_battle': False,
            'max_continuous': 0,
        })
        self._click_rich_man_challenge(self.I_RM_MODE_FIGHT, mode='normal')
        self.run_general_battle(
            battle,
            battle_key=f'rich_man_fight_{self.action_count["rich_man"]}',
            exit_matcher=self.I_RM_THROW,
        )

    def _run_rich_man_boss_fight(self, switch_loadout: bool):
        logger.hr('RichMan boss fight task', 3)
        self._enter_boss_fight_by_anchor()
        source = self.battle_config('rich_man')
        battle = source.copy(update={
            'lock_team_enable': False,
            'preset_enable': source.preset_enable and switch_loadout,
            'continuous_battle': False,
            'max_continuous': 0,
        })
        self._click_rich_man_challenge(self.I_RM_MODE_FIGHT_BOSS, mode='boss')
        self.run_general_battle(
            battle,
            battle_key=f'rich_man_boss_{time.monotonic_ns()}',
            exit_matcher=self.I_RM_THROW,
        )
        self._close_boss_level_up()

    def _close_boss_level_up(self):
        level_up_seen = False
        while True:
            self.screenshot()
            if self.appear(self.I_LEVEL_UP):
                level_up_seen = True
                self.click(self.C_RM_RANDOM_CLOSE_SAFE, interval=1)
                continue
            if level_up_seen:
                return
            time.sleep(0.3)

    def _enter_boss_fight_by_anchor(self):
        for attempt in range(1, 3):
            self.screenshot()
            if self.appear(self.I_RM_MODE_FIGHT_BOSS):
                return
            if not self._boss_anchor_appear():
                time.sleep(0.3)
                continue
            x, y, width, height = self.I_RM_FITGHT_ANCHOR.roi_front
            click_x = max(0, min(1279, x + width // 2))
            click_y = max(0, min(719, y + height // 2 - 70))
            self.device.click(x=click_x, y=click_y, control_name='rm_boss_fight_dynamic_enter')
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                self.screenshot()
                if self.appear(self.I_RM_MODE_FIGHT_BOSS):
                    return
                time.sleep(0.3)
        raise GameStuckError('RichMan boss entry failed after 2 dynamic click attempts')

    def _click_rich_man_challenge(self, challenge_button, mode: str):
        self.screenshot()
        if self.appear_then_click(challenge_button):
            logger.info(f'Click RichMan {mode} fight challenge')
        else:
            logger.warning(f'RichMan {mode} fight challenge button not found')

    def _sync_rich_man_team_lock(self, switch_loadout: bool) -> bool:
        conf = self.battle_config('rich_man')
        should_lock = conf.lock_team_enable and not (conf.preset_enable and switch_loadout)
        return self.ui_click_until_appear(self.I_RM_MAIN_UNLOCK,
                   page_check=self.I_RM_THROW,
                   expected_present=not (should_lock))

    def _wait_for_stable_throw_before_next_round(self):
        """连续确认投掷键稳定出现，并随机停顿后才允许下一轮投掷。"""
        stable_count = 0
        while True:
            self.screenshot()
            if self.appear(self.I_RM_THROW):
                stable_count += 1
                if stable_count >= 2:
                    delay = random.uniform(0.5, 1.5)
                    logger.info(
                        f'RichMan throw button stable; wait {delay:.2f}s '
                        'before next throw cycle'
                    )
                    time.sleep(delay)
                    return
                time.sleep(0.3)
                continue
            stable_count = 0
            if self.ui_reward_appear_click():
                continue
            if self.appear_then_click(self.I_UI_CONFIRM, interval=1) or \
                    self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1):
                continue
            time.sleep(0.5)
