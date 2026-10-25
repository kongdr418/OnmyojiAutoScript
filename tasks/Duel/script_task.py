# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import monotonic, sleep

import random
from datetime import time, datetime, timedelta

from module.logger import logger
from module.exception import TaskEnd
from module.base.timer import Timer
from module.base.protect import random_sleep

from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.SwitchOnmyoji.switch_onmyoji import SwitchOnmyoji
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_duel, page_onmyodo, random_click
from tasks.Duel.config import Duel
from tasks.Duel.assets import DuelAssets
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.page import page_main, page_shikigami_records

""" 斗技 """


class ScriptTask(GameUi, GeneralBattle, SwitchSoul, DuelAssets, SwitchOnmyoji):
    # TODO: 斗技适配页面模块

    battle_win_count = 0
    battle_lose_count = 0
    # 名士分段视为普通斗技分数 > 3000；3001仅作比较下界，不代表实测分数。
    CELEB_SCORE_LOWER_BOUND = 3001
    current_score = 0
    current_celeb_star = 0
    current_normal_honor = 0
    current_normal_honor_total = 0
    current_celeb_honor = 0
    current_celeb_honor_total = 0
    is_celeb: bool = False  # 是否是名士
    practice_mode: bool = False  # 当前一局是否使用练习模式测试
    conf: Duel = None

    def run(self):
        self.conf = self.config.duel
        current_time = datetime.now().time()
        if not self.conf.duel_celeb_config.practice_test and not (
            time(12, 00) <= current_time < time(23, 00)
        ):
            self.set_next_run(task='Duel', success=True, finish=False)
            raise TaskEnd('Duel')
        if self.conf.duel_celeb_config.practice_test and not (
            time(12, 00) <= current_time < time(23, 00)
        ):
            logger.info('Duel practice test is enabled outside activity time')
        limit_time = self.conf.duel_config.limit_time
        self.limit_time: timedelta = timedelta(hours=limit_time.hour, minutes=limit_time.minute,
                                               seconds=limit_time.second)
        self._weekly_goal_reached = False
        self.prepare_duel()
        while True:
            self.screenshot()
            if self.dismiss_duel_main_useless_message():
                continue
            self.check_and_get_reward()
            if not self.duel_main():
                self.goto_page(page_duel)
                continue
            if not self.can_start_duel():
                break
            if self.conf.duel_config.random_sleep:
                random_sleep(probability=0.2)
                if datetime.now() - self.start_time >= self.limit_time:
                    logger.info('Duel task is over time after random sleep')
                    break
            self.start_duel()
        logger.info('Duel battle end')
        self.finish_task_navigation()
        if self._weekly_goal_reached:
            self.set_next_run_next_monday(
                task='Duel', scheduler=self.conf.scheduler,
            )
        else:
            self.set_next_run(task='Duel', success=True, finish=True)
        raise TaskEnd('Duel')

    def prepare_duel(self):
        """斗技准备工作(切换御魂or阴阳师...), 最后回到斗技主界面"""
        self.switch_soul()
        if self.conf.duel_config.switch_enabled:
            self.goto_page(page_onmyodo)
            self.switch_onmyoji(self.conf.duel_config.switch_onmyoji)
        self.goto_page(page_duel)
        self.screenshot()
        self.dismiss_duel_main_useless_message()
        self.switch_all_soul()
        self.current_score = 0
        self.current_celeb_star = 0
        self.current_normal_honor = 0
        self.current_normal_honor_total = 0
        self.current_celeb_honor = 0
        self.current_celeb_honor_total = 0

    def can_start_duel(self) -> bool:
        """是否可以运行斗技"""
        # 任务执行时间超过限制时间，退出
        if datetime.now() - self.start_time >= self.limit_time:
            logger.info('Duel task is over time')
            return False

        practice_available = (
            self.appear(self.I_BATTLE_WITH_TRAIN)
            or self.appear(self.I_BATTLE_WITH_TRAIN2)
        )
        if self.conf.duel_celeb_config.practice_test:
            # 测试模式本身也是约束：开启后必须检测到练习入口，不能回退到正式斗技。
            if not practice_available:
                self.practice_mode = False
                logger.info('Duel practice test is enabled, but practice entrance is unavailable')
                return False
            self.practice_mode = True
            logger.info('Duel practice entrance detected; continue checking all Duel constraints')
        else:
            self.practice_mode = False
            if practice_available:
                return False

        self.update_duel_status()
        if self.is_celeb:
            if not self.conf.duel_celeb_config.celeb_battle:
                logger.info(
                    'O_D_CELEB detected 名士, but celeb battle switch is disabled'
                )
                if not self.practice_mode:
                    # 名士已经超过普通斗技最高目标3000分；不打名士时
                    # 本周普通目标已完成，不能回落到次日调度。
                    self._weekly_goal_reached = True
                    logger.info('Duel normal score goal reached by entering celeb rank; schedule next Monday')
                return False
            logger.info('O_D_CELEB detected 名士 and celeb battle switch is enabled')
            if (
                self.conf.duel_config.honor_full_exit
                and self.is_celeb_honor_full()
            ):
                logger.info('Duel normal honor and celeb honor are both full')
                self._weekly_goal_reached = True
                return False
            target_star = self.conf.duel_celeb_config.celeb_star
            if target_star > 0 and self.current_celeb_star >= target_star:
                logger.info(
                    f'Duel celeb star target reached: '
                    f'{self.current_celeb_star}/{target_star}'
                )
                self._weekly_goal_reached = True
                return False
        else:
            if self.conf.duel_config.honor_full_exit and self.check_honor():
                logger.info('Duel normal honor is full')
                self._weekly_goal_reached = True
                return False
            # 普通斗技目标分数最大按 3000 计算。
            configured_target = self.conf.duel_config.target_score
            target_score = min(configured_target, 3000)
            if configured_target > 3000:
                logger.info(f'Duel target score {configured_target} is treated as 3000')
            if self.current_score >= target_score:
                logger.info('Duel task is over score')
                self._weekly_goal_reached = True
                return False
        return True

    def start_duel(self):
        """进行一次斗技"""
        logger.hr('Duel battle', 2)
        self.current_count += 1
        self.enter_battle()
        self.battle_prepare()
        battle_ret = self.wait_battle()
        if battle_ret:
            self.battle_win_count += 1
        else:
            self.battle_lose_count += 1
        task_run_time_seconds = timedelta(seconds=int((datetime.now() - self.start_time).total_seconds()))
        logger.info(f'battle result: {battle_ret}')
        logger.info(f'battle count:{self.current_count} | win:{self.battle_win_count} failure:{self.battle_lose_count}')
        logger.info(f'battle time: {task_run_time_seconds} / {self.limit_time}')
        self.goto_page(page_duel)

    def enter_battle(self):
        """点击开始战斗(一直到出现战斗准备界面)"""
        logger.hr('duel battle matching')
        if self.practice_mode:
            self.enter_practice_ban_mode()
        waiting_started_at = None
        waiting_logged = False
        matchmaking_timeout = float(
            getattr(self.conf.duel_config, 'matchmaking_timeout_seconds', 60)
        )
        while not self.is_in_battle_prepare():
            # 排队和匹配成功后的加载都可能超过全局60秒卡死限制。
            self.device.stuck_record_clear()
            self.screenshot()
            if self.appear(self.I_EXIT_WAITING):
                now = monotonic()
                if waiting_started_at is None:
                    waiting_started_at = now
                if not waiting_logged:
                    logger.info(
                        'Duel matchmaking: waiting for opponent; '
                        f'limit={matchmaking_timeout:.0f}s'
                    )
                    waiting_logged = True
                if now - waiting_started_at < matchmaking_timeout:
                    sleep(0.5)
                    continue

                # 超时点重新截图，避免“刚好匹配成功”时仍按旧帧点击取消。
                self.device.stuck_record_clear()
                self.screenshot()
                if not self.appear(self.I_EXIT_WAITING):
                    logger.info(
                        'Duel matchmaking completed while timeout exit was '
                        'being verified; abort cancellation'
                    )
                    waiting_started_at = None
                    waiting_logged = False
                    continue
                if not self.appear_then_click(self.I_EXIT_WAITING, interval=0):
                    self.device.stuck_record_clear()
                    self.screenshot()
                    logger.info(
                        'Duel matchmaking exit click was not executed; '
                        'refresh state before continuing'
                    )
                    if self.is_in_battle_prepare(skip_screenshot=True) or not \
                            self.appear(self.I_EXIT_WAITING):
                        logger.info(
                            'Duel entered battle while matchmaking exit '
                            'failed; abort cancellation'
                        )
                        waiting_started_at = None
                        waiting_logged = False
                    continue

                # 点击并不代表退出成功。任一退出步骤未得到确认时都以新帧
                # 复核：仍在等待则稍后重试；未回大厅则视为竞态匹配成功。
                sleep(0.5)
                self.device.stuck_record_clear()
                self.screenshot()
                if self.is_in_battle_prepare(skip_screenshot=True):
                    logger.info(
                        'Duel entered battle during matchmaking exit; '
                        'abort cancellation'
                    )
                    return
                if self.appear(self.I_EXIT_WAITING):
                    logger.warning(
                        'Duel matchmaking exit was not completed; retry'
                    )
                    continue
                if not (
                    self.appear(self.I_D_BATTLE)
                    or self.appear(self.I_D_BATTLE2)
                    or self.appear(self.I_D_BATTLE_PROTECT)
                ):
                    logger.info(
                        'Duel matchmaking marker disappeared without '
                        'returning to lobby; treat as matched and wait for '
                        'battle entry'
                    )
                    waiting_started_at = None
                    waiting_logged = False
                    continue
                logger.warning(
                    'Duel matchmaking timeout; exited and will retry: '
                    f'limit={matchmaking_timeout:.0f}s'
                )
                waiting_started_at = None
                waiting_logged = False
                sleep(0.5)
                continue

            if waiting_started_at is not None:
                logger.info(
                    'Duel matchmaking marker disappeared naturally; '
                    'match succeeded, wait for battle entry'
                )
                waiting_started_at = None
                waiting_logged = False
            # 战斗按钮
            self.ui_click_until_disappear(self.I_D_BATTLE, interval=1.2)
            self.ui_click_until_disappear(self.I_D_BATTLE2, interval=1.2)
            # 战斗带保护的按钮
            self.ui_click_until_disappear(self.I_D_BATTLE_PROTECT, interval=1.2)

    def enter_practice_ban_mode(self):
        """从练习入口选择 Ban 模式，随后复用正式斗技准备流程。"""
        self.screenshot()
        entered = self.appear_then_click(self.I_BATTLE_WITH_TRAIN, interval=1.2) or \
            self.appear_then_click(self.I_BATTLE_WITH_TRAIN2, interval=1.2)
        if not entered:
            logger.warning('Duel practice entrance disappeared before click')
            return

        logger.info('Enter Duel practice mode, wait for mode selection')
        sleep(2)
        self.screenshot()
        self.click(self.C_SELECT_BAN, interval=1.2)
        self.is_celeb = True
        logger.info('Select Duel practice Ban mode')
        sleep(2)

    def duel_position_rules(self, position: int):
        """返回1～5号位置对应的点击和 OCR 规则。"""
        click_rules = (
            self.C_DUEL_CLICK_1,
            self.C_DUEL_CLICK_2,
            self.C_DUEL_CLICK_3,
            self.C_DUEL_CLICK_4,
            self.C_DUEL_CLICK_5,
        )
        ocr_rules = (
            self.O_D_NAME_1,
            self.O_D_NAME_2,
            self.O_D_NAME_3,
            self.O_D_NAME_4,
            self.O_D_NAME_5,
        )
        return click_rules[position - 1], ocr_rules[position - 1]

    @staticmethod
    def _normalize_ocr_text(value) -> str:
        """统一 OCR 比较文本，忽略空白字符。"""
        if value is None:
            return ''
        return ''.join(str(value).split())

    @classmethod
    def _fuzzy_text_match(
        cls,
        expected,
        current,
    ) -> tuple[bool, float, float]:
        """Duel 内部 OCR 编辑距离兜底，不依赖公共 RuleOcr 接口。"""
        expected = cls._normalize_ocr_text(expected)
        current = cls._normalize_ocr_text(current)
        threshold = 0.75 if len(expected) <= 2 else 0.65
        if not expected or not current:
            return False, 0.0, threshold
        if expected == current:
            return True, 1.0, threshold

        # 仅保留较短字符串长度的一行动态规划状态。
        if len(expected) < len(current):
            expected, current = current, expected
        previous = list(range(len(current) + 1))
        for expected_index, expected_char in enumerate(expected, start=1):
            row = [expected_index]
            for current_index, current_char in enumerate(current, start=1):
                row.append(min(
                    row[current_index - 1] + 1,
                    previous[current_index] + 1,
                    previous[current_index - 1]
                    + (expected_char != current_char),
                ))
            previous = row
        similarity = 1.0 - previous[-1] / max(len(expected), len(current))
        return similarity >= threshold, similarity, threshold

    def check_duel_position_banned(self, position: int, expected_name: str) -> bool:
        """检查一个指定位置；空 OCR 只重试，非空结果再进行编辑距离比较。"""
        click_rule, ocr_rule = self.duel_position_rules(position)
        attempt = 0
        while True:
            attempt += 1
            self.click(click_rule, interval=0.8)
            sleep(0.8)
            self.screenshot()
            current_name = self._normalize_ocr_text(ocr_rule.ocr(self.device.image))
            if not current_name:
                logger.info(
                    f'Duel slot {position}, OCR is empty on attempt {attempt}; '
                    'click the slot and retry'
                )
                continue

            name_matched, similarity, threshold = self._fuzzy_text_match(
                expected_name, current_name
            )
            logger.info(
                f'Duel slot {position}, OCR:{current_name}, expected:{expected_name}, '
                f'similarity:{similarity:.2f}, threshold:{threshold:.2f}, '
                f'matched:{name_matched}'
            )
            if name_matched:
                return False

            logger.warning(
                f'Duel slot {position} is {current_name}, not {expected_name}; '
                'configured shikigami may be banned'
            )
            return True

    def check_celeb_shikigami_banned(self) -> bool:
        """按 OASX 配置选择单位置检查或完整阵容顺序检查。"""
        if self.conf.duel_celeb_config.full_lineup_check:
            lineup_names = [
                name.strip()
                for name in self.conf.duel_celeb_config.full_lineup_names.split(',')
                if name.strip()
            ]
            if len(lineup_names) != 5:
                logger.warning(
                    'Full Duel lineup must contain exactly five names separated by English commas, '
                    f'current:{lineup_names}'
                )
                return False
            logger.info(f'Use full Duel lineup check in deployment order: {lineup_names}')
            for position, expected_name in enumerate(lineup_names, start=1):
                if self.check_duel_position_banned(position, expected_name):
                    return True
            logger.info('All five Duel positions matched the configured full lineup')
            return False

        check_value = self.conf.duel_celeb_config.ban_check.strip()
        if not check_value:
            logger.warning('Duel Ban check is empty, skip shikigami check')
            return False

        parts = [part.strip() for part in check_value.split(',', maxsplit=1)]
        if len(parts) != 2 or not parts[1]:
            logger.warning(
                f'Invalid Duel Ban check: {check_value}, expected format: position,shikigami name'
            )
            return False
        try:
            position = int(parts[0])
        except ValueError:
            logger.warning(
                f'Invalid Duel Ban position: {parts[0]}, expected an integer from 1 to 5'
            )
            return False
        if position not in range(1, 6):
            logger.warning(
                f'Invalid Duel Ban position: {position}, expected an integer from 1 to 5'
            )
            return False
        return self.check_duel_position_banned(position, parts[1])

    def battle_prepare(self):
        """选式神准备斗技阶段"""
        logger.hr('duel battle preparing')
        not_in_prepare_cnt, max_retry = 0, 3
        celeb_ban_checked = False
        celeb_ban_confirmed = False
        celeb_selection_ready = False
        celeb_selection_timer = Timer(30)
        while True:
            if not_in_prepare_cnt >= max_retry:  # max_retry次识别不到任何阶段元素(准备,战斗,结算), 退出
                break
            self.screenshot()
            if self.is_battle_end() or self.is_in_real_battle():  # 战斗已经结束或已经开始战斗
                break

            # 点击禁选后等待 0.1 秒并刷新截图；只有 I_BAN_ENSURE 出现才确认禁选成功。
            if not celeb_ban_confirmed and self.appear_then_click(self.I_BAN, interval=1.2):
                self.is_celeb = True
                sleep(0.1)
                self.screenshot()
                if self.appear(self.I_BAN_ENSURE):
                    celeb_ban_confirmed = True
                    celeb_selection_timer.start()
                    logger.info(
                        'I_BAN_ENSURE appeared, Ban confirmed; '
                        'refresh screenshot every 2s and wait for I_D_WORD_BATTLE'
                    )
                    sleep(2)
                else:
                    logger.info('I_BAN_ENSURE not found after Ban click, retry Ban')
                continue

            # 禁选确认后不再点击 I_BAN；每 2 秒刷新一次，等待顶部“战”字出现。
            if celeb_ban_confirmed and not celeb_selection_ready:
                if self.appear(self.I_D_WORD_BATTLE):
                    logger.info('I_D_WORD_BATTLE appeared, start configured shikigami check')
                    celeb_selection_ready = True
                else:
                    if celeb_selection_timer.reached():
                        logger.warning(
                            'I_D_WORD_BATTLE did not appear within 30s after Ban, exit this duel'
                        )
                        self.duel_exit_battle()
                        return
                    sleep(2)
                    continue

            # 已知是名士但尚未完成禁选确认时，不允许提前执行式神核对。
            if self.is_celeb and not celeb_ban_confirmed and not celeb_selection_ready:
                if not celeb_selection_timer.started():
                    celeb_selection_timer.start()
                if celeb_selection_timer.reached():
                    logger.warning('Cannot confirm Duel Ban within 30s, exit this duel')
                    self.duel_exit_battle()
                    return
                sleep(0.5)
                continue

            if not celeb_selection_ready and not self.is_in_battle_prepare():  # 非名士的普通准备阶段
                not_in_prepare_cnt += 1
                sleep(random.uniform(1.2, 2.4))
                continue
            not_in_prepare_cnt = 0

            # 每场只核对一次配置的式神位置。
            if self.is_celeb and celeb_selection_ready and not celeb_ban_checked:
                celeb_ban_checked = True
                if self.check_celeb_shikigami_banned():
                    self.duel_exit_battle()
                    return

            # 配置位置仍是目标式神，继续普通斗技的自动上阵和准备流程。
            if self.appear_then_click(self.I_D_AUTO_ENTRY, interval=1.2) or \
                    self.appear_then_click(self.I_D_PREPARE, interval=1.2):
                self.reset_device('PREPARE_BEFORE_BATTLE')

    def wait_battle(self) -> bool:
        """等待战斗结束, 返回战斗结果, 最后会退出到斗技主界面"""
        logger.hr('duel battle waiting')
        battle_operated = False
        battle_timeout_timer = Timer(270).start()
        ret_timer = Timer(5)
        battle_timeout_cnt, max_timeout_cnt = 0, 3
        ret = None
        while True:
            self.screenshot()
            self.check_and_get_reward()
            if self.appear(self.I_CHECK_DUEL) and self.appear(self.I_D_HELP):  # 斗技主界面
                break
            if self.appear(self.I_D_WIN_SHARE,interval= 1.2): #拔得头筹
                self.click(random_click(ltrb=(True, True, False, True)), interval=1.2)
                continue
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1.2):  # 关闭段位上升页面
                ret_timer.reset()
                continue
            if ret_timer.started() and ret_timer.reached():  # 兜底逻辑, 已经结算了但是还没有到斗技主界面
                self.goto_page(page_duel)
                break
            if self.is_battle_win():
                ret = True
                ret_timer.start()
                self.click(random_click(ltrb=(True, True, False, True)), interval=1.2)
                continue
            if self.is_battle_lose():
                ret = False
                ret_timer.start()
                self.click(random_click(ltrb=(True, True, False, True)), interval=1.2)
                continue
            if not ret_timer.started() and battle_timeout_cnt >= max_timeout_cnt:
                logger.warning('Duel battle timeout[>15 minutes], exit')
                self.duel_exit_battle()
                continue
            if ret is None and not battle_operated:  # 进行战斗前的操作
                self.ui_click(self.O_BATTLE_HAND, self.O_BATTLE_AUTO, interval=0.8)
                self.green_mark(self.conf.duel_config.green_enable, self.conf.duel_config.green_mark)
                battle_operated = True
                self.reset_device('BATTLE_STATUS_S')
                continue
            if not ret_timer.started() and battle_timeout_timer.reached_and_reset():
                battle_timeout_cnt += 1
                self.reset_device('BATTLE_STATUS_S')
                logger.warning("battle' time is too long, increase wait time")
        return ret

    def duel_exit_battle(self):
        while 1:
            self.screenshot()
            if self.appear(self.I_D_FAIL) or self.appear(self.I_FALSE):
                return
            if self.appear_then_click(self.I_EXIT_ENSURE):
                continue
            # 选式神界面退出或战斗内退出
            if self.appear_then_click(self.I_DUEL_EXIT, interval=1) or self.appear_then_click(self.I_EXIT, interval=1):
                continue

    def read_celeb_honors(self) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """分别读取名士界面的普通荣誉和名士荣誉比例数。"""
        default_counter = (0, 0, 0)
        normal_icon_found = self.appear(self.I_DUEL_HONOR)
        celeb_icon_found = self.appear(self.I_DUEL_CELEB_HONOR)
        if not normal_icon_found or not celeb_icon_found:
            logger.warning(
                'Duel honor labels not found: '
                f'normal={normal_icon_found}, celeb={celeb_icon_found}'
            )
            return default_counter, default_counter

        original_roi = list(self.O_D_CELEB_HONOR.roi)
        roi_left, roi_top, roi_width, roi_height = original_roi
        roi_right = roi_left + roi_width

        def read_after_icon(icon, right: int) -> tuple[int, int, int]:
            icon_x, _, icon_width, _ = icon.roi_front
            left = max(roi_left, icon_x + icon_width)
            right = min(roi_right, right)
            if right <= left:
                logger.warning(f'Invalid Duel honor OCR range: {left}-{right}')
                return default_counter
            self.O_D_CELEB_HONOR.roi = [left, roi_top, right - left, roi_height]
            return self.O_D_CELEB_HONOR.ocr(self.device.image)

        try:
            celeb_icon_x = self.I_DUEL_CELEB_HONOR.roi_front[0]
            normal_counter = read_after_icon(self.I_DUEL_HONOR, celeb_icon_x)
            celeb_counter = read_after_icon(self.I_DUEL_CELEB_HONOR, roi_right)
        finally:
            self.O_D_CELEB_HONOR.roi = original_roi

        return normal_counter, celeb_counter

    def update_celeb_honors(self) -> None:
        """刷新两组荣誉计数，并保留各自的当前值和上限。"""
        normal_counter, celeb_counter = self.read_celeb_honors()
        normal_current, _, normal_total = normal_counter
        celeb_current, _, celeb_total = celeb_counter
        self.current_normal_honor = normal_current
        self.current_normal_honor_total = normal_total
        self.current_celeb_honor = celeb_current
        self.current_celeb_honor_total = celeb_total
        logger.info(
            f'Duel honors: normal {normal_current}/{normal_total}, '
            f'celeb {celeb_current}/{celeb_total}'
        )

    def check_honor(self) -> bool:
        """检查荣誉是否满了"""
        if self.is_celeb:
            self.update_celeb_honors()
            return (
                self.current_normal_honor_total > 0
                and self.current_normal_honor >= self.current_normal_honor_total
            )
        if not self.appear(self.I_DUEL_HONOR):
            return False
        roi_x = self.I_DUEL_HONOR.roi_front[0] + self.I_DUEL_HONOR.roi_front[2]
        roi_y = self.I_DUEL_HONOR.roi_front[1]
        roi_w = 110
        roi_h = self.I_DUEL_HONOR.roi_front[3]
        self.O_D_HONOR.roi = [roi_x, roi_y, roi_w, roi_h]
        current, remain, total = self.O_D_HONOR.ocr(self.device.image)
        return total > 0 and current >= total and remain == 0

    def is_celeb_honor_full(self) -> bool:
        """普通荣誉和名士荣誉两组比例数都满时才返回 True。"""
        return (
            self.current_normal_honor_total > 0
            and self.current_normal_honor >= self.current_normal_honor_total
            and self.current_celeb_honor_total > 0
            and self.current_celeb_honor >= self.current_celeb_honor_total
        )

    def update_duel_status(self, skip_screenshot: bool = True):
        """
        更新斗技主界面状态。

        O_D_CELEB 文本中包含“名士”时进入名士分支，并从同一文本提取星星数，
        同时读取名士荣誉；
        否则进入普通斗技分支，只读取普通斗技分数。

        :param skip_screenshot: 是否跳过截图
        """
        self.maybe_screenshot(skip_screenshot)
        celeb_text = self.O_D_CELEB.ocr(self.device.image).strip()
        self.is_celeb = '名士' in celeb_text
        logger.info(f'Duel type OCR: {celeb_text or "<empty>"}, is celeb:{self.is_celeb}')

        if self.is_celeb:
            self.current_score = self.CELEB_SCORE_LOWER_BOUND
            star_text = ''.join(char for char in celeb_text if char.isdigit())
            self.current_celeb_star = int(star_text) if star_text else 0
            self.update_celeb_honors()
            logger.info(f'Duel celeb star: {self.current_celeb_star}')
            return

        score, remain, total = self.O_D_SCORE.ocr(self.device.image)
        if score > 10000:
            # 识别错误分数超过一万, 去掉最高位
            logger.warning('Recognition error, score is too high')
            score = int(str(score)[1:])
        self.current_score = score
        self.current_normal_honor = 0
        self.current_normal_honor_total = 0
        self.current_celeb_honor = 0
        self.current_celeb_honor_total = 0
        logger.info(f'battle score: {score}')

    def switch_soul(self):
        """从式神录界面切换御魂"""
        if self.conf.switch_soul.enable:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul(self.conf.switch_soul.switch_group_team)
        if self.conf.switch_soul.enable_switch_by_name:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul_by_name(self.conf.switch_soul.group_name, self.conf.switch_soul.team_name)

    def duel_main(self, screenshot=False) -> bool:
        """判断是否在斗技主界面"""
        if screenshot:
            self.screenshot()
        return self.appear(self.I_D_HELP) or self.appear(self.I_CHECK_DUEL) or \
            self.appear(self.I_D_CELEB_STAR) or self.appear(self.I_D_CELEB_HONOR)

    def dismiss_duel_main_useless_message(self) -> bool:
        """随机安全点击一次，关闭进入斗技时出现的附属消息页。"""
        if not self.appear(self.I_DUEL_MAIN_UESLESS_MESSAGE):
            return False
        logger.info('Duel entrance useless message detected; dismiss it')
        self.click(random_click(ltrb=(True, True, False, True)))
        return True

    def switch_all_soul(self):
        """在斗技式神备选界面一键切换所有御魂"""
        if not self.conf.duel_config.switch_all_soul:
            return
        click_count = 0  # 计数
        while 1:
            self.screenshot()
            if self.dismiss_duel_main_useless_message():
                continue
            if click_count >= 3:
                break
            if self.appear(self.I_WHITE_DOG):
                self.click(random_click(ltrb=(True, False, False, False)), interval=1.5)
                continue
            if self.appear_then_click(self.I_D_TEAM, interval=1):
                continue
            if self.appear_then_click(self.I_UI_CONFIRM, interval=0.6):
                continue
            if self.appear_then_click(self.I_D_TEAM_SWTICH, interval=1):
                click_count += 1
                continue
        logger.info('Souls Switch is complete')
        self.ui_click(self.I_UI_BACK_YELLOW, self.I_D_TEAM)

    def check_and_get_reward(self):
        """检查并收获奖励"""
        if self.appear(self.I_REWARD) or self.appear(self.I_UI_REWARD):
            if self.click(random_click(ltrb=(True, True, False, True)), interval=0.6):
                logger.info('get reward')

    def is_in_battle_prepare(self, skip_screenshot=True) -> bool:
        """是否在战斗准备界面"""
        self.maybe_screenshot(skip_screenshot)
        return self.appear(self.I_D_PREPARE) or \
            self.appear(self.I_D_AUTO_ENTRY) or \
            self.appear(self.I_BAN) or \
            self.appear(self.I_D_WORD_BATTLE) or \
            self.appear(self.I_D_CHECK_BAN)

    def is_battle_win(self) -> bool:
        return self.appear(self.I_WIN) or self.appear(self.I_D_VICTORY)

    def is_battle_lose(self) -> bool:
        return self.appear(self.I_FALSE) or self.appear(self.I_D_FAIL)

    def is_battle_end(self) -> bool:
        return self.is_battle_win() or self.is_battle_lose() or \
            self.appear(self.I_REWARD) or self.appear(self.I_UI_REWARD)

    def reset_device(self, status: str):
        self.device.click_record_clear()
        self.device.stuck_record_clear()
        self.device.stuck_record_add(status)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas3')
    d = Device(c)
    t = ScriptTask(c, d)

    t.run()
