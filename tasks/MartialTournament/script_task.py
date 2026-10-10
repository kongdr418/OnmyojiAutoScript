# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from datetime import datetime
import random
import time

from cached_property import cached_property

from module.atom.image import RuleImage
from module.base.protect import random_sleep
from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger

from tasks.base_task import BaseTask
from tasks.MartialTournament.assets import MartialTournamentAssets
from tasks.MartialTournament.config import MartialTournament, GeneralBattleConfig
from tasks.Component.GeneralBattle.general_battle import GeneralBattle, ExitMatcher
from tasks.Component.BaseActivity.base_activity import BaseActivity
from tasks.Component.QuickLoadout.quick_loadout import QuickLoadout
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
import tasks.MartialTournament.page as pages
from tasks.GameUi.default_pages import random_click
from typing import Optional, Callable


class LimitTimeOut(Exception):
    pass


class LimitCountOut(Exception):
    pass


class TicketsNotEnough(Exception):
    pass


class ScriptTask(GameUi, GeneralBattle, SwitchSoul, QuickLoadout, BaseActivity, MartialTournamentAssets):

    AP_COST = 30

    @cached_property
    def conf(self) -> MartialTournament:
        return self.config.model.martial_tournament

    @property
    def current_mode(self) -> str:
        """当前爬塔类型: 'pass' 或 'ap'"""
        return getattr(self, '_current_mode', 'pass')

    @current_mode.setter
    def current_mode(self, value):
        self._current_mode = value

    @property
    def current_count(self) -> int:
        return getattr(self, '_current_count', 0)

    @current_count.setter
    def current_count(self, value):
        self._current_count = value

    @property
    def pre_tickets_map(self) -> dict:
        """上一次识别的票数量, 格式: {'pass_1': -1, 'pass_2': -1, 'ap': -1}"""
        if not hasattr(self, '_pre_tickets_map'):
            self._pre_tickets_map = {'pass_1': -1, 'pass_2': -1, 'ap': -1}
        return self._pre_tickets_map

    @property
    def last_soul_type(self) -> str:
        return getattr(self, '_last_soul_type', '')

    @last_soul_type.setter
    def last_soul_type(self, value):
        self._last_soul_type = value

    @property
    def team_locked(self) -> bool:
        """阵容是否已锁定, 用于跳过重复锁定"""
        return getattr(self, '_team_locked', False)

    @team_locked.setter
    def team_locked(self, value):
        self._team_locked = value

    @property
    def ticket_type(self) -> str:
        """当前门票类型: 'pass_1'普通, 'pass_2'注灵"""
        return getattr(self, '_ticket_type', 'pass_1')

    @ticket_type.setter
    def ticket_type(self, value):
        self._ticket_type = value

    @property
    def current_battle_conf(self) -> GeneralBattleConfig:
        """当前模式对应的战斗配置"""
        return getattr(self, '_current_battle_conf', self.conf.group_battle_conf)

    @current_battle_conf.setter
    def current_battle_conf(self, value):
        self._current_battle_conf = value

    @property
    def current_limit(self) -> int:
        """当前模式的次数上限"""
        if self.current_mode == 'ap':
            return self.conf.general_climb.ap_limit
        return self.conf.general_climb.pass_limit

    @property
    def current_climb_page(self) -> pages.Page:
        """当前模式对应的页面"""
        if self.current_mode == 'ap':
            return pages.page_mt_ap
        return pages.page_mt_pass

    def _exit_matcher(self) -> ExitMatcher | None:
        """战斗结束检测: 根据当前模式检测对应界面标志"""
        if self.current_mode == 'ap':
            return self.I_MT_CHALLENGE_AP
        return self.I_MT_SEARCH

    def before_run(self):
        pages.page_battle_result = self.navigator.resolve_page(pages.page_battle_result)

    @property
    def mt_page_handle_dict(self) -> dict[pages.Page, Callable]:
        """活动页面和处理器的映射"""
        return {
            pages.page_mt_pass: self._run_pass,
            pages.page_mt_ap: self._run_ap,
            pages.page_battle_prepare: lambda: self.run_general_battle(self.current_battle_conf,
                                                                       battle_key='mt'),
            pages.page_battle: lambda: self.run_general_battle(self.current_battle_conf,
                                                               battle_key='mt'),
            pages.page_reward: lambda: self.click(random_click(ltrb=(False, False, True, False)), interval=1.5),
        }

    def run(self):
        self.before_run()
        try:
            for mode in self.conf.general_climb.sequence_list:
                self.current_mode = mode
                self.current_count = 0
                self.last_soul_type = ''
                self.team_locked = False
                self.ticket_type = 'pass_1'
                self.goto_page(self.current_climb_page)
                logger.hr(f'Start climb mode: {mode}', 2)
                try:
                    while True:
                        self.screenshot()
                        self.update_status()
                        current_page = self.get_current_page()
                        if current_page is None:
                            time.sleep(0.5)
                            continue
                        handle = self.mt_page_handle_dict.get(current_page, None)
                        if handle is None:
                            self.goto_page(self.current_climb_page)
                            continue
                        handle()
                except LimitCountOut:
                    logger.info(f'Climb mode {mode} count limit reached, switch to next mode')
                    continue
                except TicketsNotEnough:
                    logger.info(f'Climb mode {mode} tickets not enough, switch to next mode')
                    continue
        except LimitTimeOut:
            pass
        self.goto_page(pages.page_main)
        if self.conf.general_climb.active_souls_clean:
            self.set_next_run(task='SoulsTidy', success=False, finish=False, target=datetime.now())
        self.set_next_run(task="MartialTournament", success=True)
        raise TaskEnd

    def update_status(self):
        """更新全局状态, 检查是否超时或达到次数限制"""
        if datetime.now() - self.start_time >= self.conf.general_climb.limit_time_v:
            logger.info('MartialTournament time out')
            raise LimitTimeOut
        if self.current_count >= self.current_limit:
            logger.info(f'MartialTournament[{self.current_mode}] count limit reached')
            raise LimitCountOut

    def _run_pass(self):
        """日清门票界面处理: 已发现boss直接进, 否则检查门票后搜索"""
        # 先检测是否有已发现的boss (I_NO_SEARCH可见), 有则直接进入
        self.screenshot()
        if self.appear(self.I_NO_SEARCH) and self.appear_then_click(self.I_SEARCH_BOSS, interval=1.5):
            logger.info('Found existing boss, enter challenge directly')
        else:
            # 没有已发现的boss, 检查门票后搜索
            if not self.check_tickets_enough():
                raise TicketsNotEnough
            pass_1, pass_2 = self._boss_ticket_counts
            if not self.search_boss_with_ticket('pass_1', pass_1) and \
                    not self.search_boss_with_ticket('pass_2', pass_2):
                raise TicketsNotEnough
        boss_type = self.detect_boss_type()
        quick_loadout_conf = self.conf.boss_quick_loadout_config
        if not self.boss_ap_enough():
            raise TicketsNotEnough
        if quick_loadout_conf.enable:
            if not self.run_quick_loadout(
                quick_loadout_conf,
                entry=self.I_MT_GOTO_QUICK_LOADOUT,
                fight_anchor=self.I_MT_QUICK_LOADOUT_FIGHT,
                dismiss=self.C_MT_QUICK_LOADOUT_CLOSE,
                name_ocr=self.O_BOSS_NAME,
            ):
                raise TicketsNotEnough
        else:
            self.switch_soul(self.I_MT_RECORDS, boss_type)
        if self.conf.general_climb.random_sleep:
            random_sleep(probability=0.2)
        self.current_battle_conf = self.conf.single_battle_conf if boss_type == 'single' else self.conf.group_battle_conf
        if quick_loadout_conf.enable and self.current_battle_conf.preset_enable:
            logger.warning('Quick loadout enabled, disable legacy battle preset for this battle')
            self.current_battle_conf = self.current_battle_conf.model_copy(
                update={'preset_enable': False}
            )
        # 首领战每轮确认锁定状态；一键配置不受页面锁定开关影响
        self.lock_team(self.current_battle_conf)
        self.team_locked = True
        if self.enter_battle():
            # current_count由run_general_battle内部递增, 不需要手动+1
            self.run_general_battle(self.current_battle_conf, battle_key=f'mt_{boss_type}')

    def _run_ap(self):
        """体力爬塔界面处理: 检查体力 -> 切换御魂 -> 锁定阵容 -> 挑战 -> 战斗"""
        if not self.check_tickets_enough():
            raise TicketsNotEnough
        self.switch_soul(self.I_MT_RECORDS, 'ap')
        if self.conf.general_climb.random_sleep:
            random_sleep(probability=0.2)
        self.current_battle_conf = self.conf.mt_ap_battle_conf
        # 体力界面锁定/解锁阵容 (只需锁定一次)
        if not self.team_locked:
            self.lock_team(self.current_battle_conf)
            self.team_locked = True
        entered = self.enter_ap_battle()
        if not entered:
            raise TicketsNotEnough
        # current_count由run_general_battle内部递增, 不需要手动+1
        self.run_general_battle(self.current_battle_conf, battle_key='mt')

    def detect_ticket_type(self):
        """检测门票类型: I_PASS_2可见=注灵, 否则=普通"""
        if self.appear(self.I_PASS_2):
            self.ticket_type = 'pass_2'
        else:
            self.ticket_type = 'pass_1'

    def boss_ap_enough(self) -> bool:
        """首领挑战浮窗打开后、开始挑战前检测体力。"""
        best_ap = 0
        for attempt in range(1, 4):
            self.screenshot()
            ap = int(self.O_BOSS_AP_COUNT.ocr(self.device.image) or 0)
            best_ap = max(best_ap, ap)
            logger.info(f'MartialTournament boss AP OCR {attempt}/3: AP={ap}')
            if ap > self.AP_COST:
                return True
            if attempt < 3:
                time.sleep(0.5)
        logger.info(f'MartialTournament boss AP insufficient: {best_ap} <= {self.AP_COST}')
        return False

    def search_boss(self, max_times: int | None = None) -> bool:
        """搜索boss, 等待挑战浮窗出现"""
        logger.hr('Search boss', 2)
        search_times = 0
        max_times = max_times or random.randint(3, 5)
        wait_timer = Timer(10).start()
        click_limit_logged = False
        while True:
            self.screenshot()
            if self.appear(self.I_MT_CHALLENGE):
                logger.info('Search boss success')
                return True
            if wait_timer.reached():
                logger.warning('Search boss timeout')
                return False
            if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1) or \
                    self.click_ui_confirm(interval=1):
                continue
            if search_times >= max_times:
                if not click_limit_logged:
                    logger.info(
                        f'Search boss click limit reached ({search_times}/{max_times}), '
                        'continue waiting for challenge panel'
                    )
                    click_limit_logged = True
                time.sleep(0.2)
                continue
            # 根据检测到的门票类型选择搜索按钮
            search_btn = self.I_PASS_2 if self.ticket_type == 'pass_2' else self.I_MT_SEARCH
            if self.appear_then_click(search_btn, interval=1.5):
                search_times += 1
                logger.info(f'Try search boss ({self.ticket_type}), remain times[{max_times - search_times}]')
                continue
        return False

    def search_boss_with_ticket(self, ticket_type: str, ticket_count: int) -> bool:
        """单独校验一种门票，并执行对应的正常或保底搜寻流程。"""
        self.ensure_ticket_mode(ticket_type)
        if ticket_count > 0:
            logger.info(
                f'MartialTournament {ticket_type}={ticket_count}, use normal search flow'
            )
            return self.search_boss()
        return self.verify_zero_ticket(
            f'MartialTournament {ticket_type}',
            lambda: self.search_boss(max_times=1),
        )

    def enter_battle(self) -> bool:
        """点击挑战按钮进入战斗 (挑战界面为浮窗)"""
        logger.hr('Enter battle', 2)
        click_times, max_times = 0, random.randint(3, 5)
        while True:
            self.screenshot()
            if self.is_in_battle(False):
                return True
            if click_times >= max_times:
                logger.warning('Cannot enter battle, click reach max times')
                raise TicketsNotEnough
            if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1) or \
                    self.click_ui_confirm(interval=1):
                continue
            if self.appear_then_click(self.I_MT_CHALLENGE, interval=1.5):
                self.device.click_record_clear()
                click_times += 1
                logger.info(f'Try click challenge, remain times[{max_times - click_times}]')
                continue
        return False

    def enter_ap_battle(self, max_times: int | None = None) -> bool:
        """点击体力界面的挑战按钮进入战斗"""
        logger.hr('Enter AP battle', 2)
        click_times = 0
        fallback = max_times is not None
        max_times = max_times or random.randint(3, 5)
        while True:
            self.screenshot()
            if self.is_in_battle(False):
                return True
            if click_times >= max_times:
                logger.warning('Cannot enter AP battle, click reach max times')
                if fallback:
                    return False
                raise TicketsNotEnough
            if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1) or \
                    self.click_ui_confirm(interval=1):
                continue
            if self.appear_then_click(self.I_MT_CHALLENGE_AP, interval=1.5):
                self.device.click_record_clear()
                click_times += 1
                logger.info(f'Try click AP challenge, remain times[{max_times - click_times}]')
                continue
        return False

    def detect_boss_type(self) -> str:
        """检测当前浮窗中的boss类型: 'single'单体, 'group'群体"""
        self.screenshot()
        if self.appear(self.I_BOSS_1) or self.appear(self.I_BOSS_2):
            logger.info('Detect boss type: single')
            return 'single'
        logger.info('Detect boss type: group')
        return 'group'

    def switch_soul(self, enter_button: RuleImage, boss_type: str):
        """根据boss类型切换御魂
        - single: 使用单体boss御魂配置
        - group: 使用群体boss御魂配置
        - ap: 使用体力爬塔御魂配置
        类型相同则跳过, 避免重复切换
        """
        if self.last_soul_type == boss_type:
            return
        conf = self.conf.switch_soul_config
        if boss_type == 'single':
            enable = conf.enable_switch_single or conf.enable_switch_single_by_name
            if not enable:
                self.last_soul_type = boss_type
                return
        elif boss_type == 'ap':
            enable = conf.enable_switch_mt_ap or conf.enable_switch_mt_ap_by_name
            if not enable:
                self.last_soul_type = boss_type
                return
        else:  # group
            enable = conf.enable_switch_group or conf.enable_switch_group_by_name
            if not enable:
                self.last_soul_type = boss_type
                return
        logger.hr(f'Start switch soul ({boss_type})', 2)
        self.ui_click(enter_button, stop=self.I_CHECK_RECORDS, interval=1)
        if boss_type == 'single':
            if conf.enable_switch_single_by_name:
                group, team = conf.single_group_team_name.split(",")
                self.run_switch_soul_by_name(group, team)
            elif conf.enable_switch_single:
                self.run_switch_soul(conf.single_group_team)
        elif boss_type == 'ap':
            if conf.enable_switch_mt_ap_by_name:
                group, team = conf.mt_ap_team_name.split(",")
                self.run_switch_soul_by_name(group, team)
            elif conf.enable_switch_mt_ap:
                self.run_switch_soul(conf.mt_ap_team)
        else:  # group
            if conf.enable_switch_group_by_name:
                group, team = conf.group_boss_team_name.split(",")
                self.run_switch_soul_by_name(group, team)
            elif conf.enable_switch_group:
                self.run_switch_soul(conf.group_boss_team)
        self.last_soul_type = boss_type
        self.exit_shikigami_records()
        # 等待界面重新出现
        wait_target = self.I_MT_CHALLENGE if boss_type != 'ap' else self.I_MT_CHALLENGE_AP
        wait_timer = Timer(5).start()
        while not wait_timer.reached():
            self.screenshot()
            if self.appear(wait_target):
                break

    def lock_team(self, battle_conf: GeneralBattleConfig):
        """根据配置判断是否锁定阵容, 并执行锁定或解锁"""
        enable = battle_conf.lock_team_enable
        if enable:
            logger.info('Lock team')
            self.ui_click(self.I_UNLOCK, stop=self.I_LOCK, interval=1.5)
            return
        logger.info('Unlock team')
        self.ui_click(self.I_LOCK, stop=self.I_UNLOCK, interval=1.5)

    def check_tickets_enough(self) -> bool:
        """统一检查门票/体力是否足够
        pass1和pass2数量同时显示, 直接读两种券选有票的用"""
        logger.hr('Check tickets')
        self.screenshot()
        # ap模式
        if self.current_mode == 'ap':
            remain_times = int(self.O_O_AP.ocr(self.device.image) or 0)
            logger.info(f'AP remain: {remain_times}')
            if self.pre_tickets_map['ap'] - remain_times > 1:
                self.pre_tickets_map['ap'] -= 1
                return True
            self.pre_tickets_map['ap'] = remain_times
            return remain_times > 0
        # pass模式: 同时读两种券数量
        pass_1_remain = self.O_O_PASS.ocr(self.device.image)
        pass_2_remain = self.O_O_PASS2.ocr(self.device.image)
        logger.info(f'pass_1 remain: {pass_1_remain}, pass_2 remain: {pass_2_remain}')
        # 容错: 差值大于1认为识别有误
        if pass_1_remain > 0 and self.pre_tickets_map['pass_1'] - pass_1_remain > 1:
            self.pre_tickets_map['pass_1'] -= 1
            pass_1_remain = self.pre_tickets_map['pass_1']
        else:
            self.pre_tickets_map['pass_1'] = pass_1_remain
        if pass_2_remain > 0 and self.pre_tickets_map['pass_2'] - pass_2_remain > 1:
            self.pre_tickets_map['pass_2'] -= 1
            pass_2_remain = self.pre_tickets_map['pass_2']
        else:
            self.pre_tickets_map['pass_2'] = pass_2_remain
        self._boss_ticket_counts = (pass_1_remain, pass_2_remain)
        return True

    def ensure_ticket_mode(self, target_type: str) -> None:
        """确保首领搜寻处于指定门票模式。"""
        self.detect_ticket_type()
        if self.ticket_type != target_type:
            logger.info(f'Switch ticket mode to {target_type}')
            self.switch_ticket_mode()

    def switch_ticket_mode(self):
        """点击切换门票模式按钮 (I_SWITCH_MODE), 自动检测切换结果"""
        self.screenshot()
        if self.appear(self.I_SWITCH_MODE):
            self.click(self.I_SWITCH_MODE, interval=1.5)
            # 切换后截图检测当前模式
            self.screenshot()
            self.detect_ticket_type()
            # 只重置切换后的当前门票记录, 保留另一种门票的状态
            self.pre_tickets_map[self.ticket_type] = -1
            logger.info(f'Switched ticket mode to: {self.ticket_type}')
        else:
            logger.warning('I_SWITCH_MODE not found, cannot switch ticket mode')


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)
    t.run()
