from tasks.GameUi.assets import GameUiAssets
"""式神活动三种玩法共用运行能力。"""

from datetime import datetime

from cached_property import cached_property

from module.atom.image import RuleImage
from module.base.protect import random_sleep
from module.exception import TaskEnd
from module.logger import logger
from tasks.ActivityShikigami.assets import ActivityShikigamiAssets
from tasks.ActivityShikigami.config import ActivityShikigami, BATTLE_TYPES
from tasks.ActivityShikigami.settlement import settlement_random_click
from tasks.Component.BaseActivity.base_activity import BaseActivity
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.GeneralBattle.general_battle import BattleAction, BattleContext, GeneralBattle
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
import tasks.ActivityShikigami.page as pages


class ActivityResourceNotEnough(Exception):
    """当前子玩法资源不足，可以切换到下一项。"""


class BaseAct(GameUi, GeneralBattle, SwitchSoul, BaseActivity, ActivityShikigamiAssets):
    """统一保存计数、软时间边界、随机休息、御魂和战斗配置。"""

    def __init__(self, config, device):
        super().__init__(config, device)
        self.action_count = {name: 0 for name in BATTLE_TYPES}
        climb_consumables = (
            'pass', 'ap_pass', 'boss', 'ap100', 'penta_pass'
        )
        self.climb_consumable_count = {
            name: -1 for name in climb_consumables
        }
        self.climb_pending_consumption = {
            name: 0 for name in climb_consumables
        }
        self.climb_ocr_correction_rounds = {
            name: 0 for name in climb_consumables
        }
        self.penta_pass_active = False
        self.pass_action_count = {'easy': 0, 'hard': 0}
        self.current_pass_mode = None
        self.switched_soul = {name: False for name in BATTLE_TYPES}
        self.current_action_type = ''
        self.activity_time_reached = False

    @cached_property
    def conf(self) -> ActivityShikigami:
        return self.config.model.activity_shikigami

    @property
    def scheduled_task_name(self) -> str:
        return 'ActivityShikigami'

    def before_run(self):
        battle_result = self.navigator.resolve_page(pages.page_battle_result)
        battle_result.recognizer = pages.any_of(
            pages.all_of(
                lambda task: not task.current_action_type.startswith('exp_')
                or not task.appear(task.I_EVENT_FIGHT),
                self.I_UI_BACK_RED,
            ),
            battle_result.recognizer,
        )

    def _exit_matcher(self):
        if self.current_action_type == 'fakegod':
            return self.I_FG_ACT_FIRE
        if self.current_action_type in ('ap', 'pass', 'ap100', 'boss'):
            return self._climb_fire_rule(self.current_action_type)
        return None

    def _handle_result(self, context: BattleContext, config: GeneralBattleConfig) -> BattleAction:
        if self.current_action_type == 'boss':
            pages.handle_activity_close(self)
        return super()._handle_result(context, config)

    def battle_config(self, action_type: str) -> GeneralBattleConfig:
        if action_type == 'pass' and getattr(self, 'current_pass_mode', None) == 'hard':
            action_type = 'pass_hard'
        return getattr(self.conf, f'{action_type}_battle_conf')

    def _battle_settlement_click(self):
        return settlement_random_click()

    def action_limit(self, action_type: str) -> int:
        return self.conf.general_config.limit_for(action_type)

    def time_limit_reached(self) -> bool:
        reached = datetime.now() - self.start_time >= self.conf.general_config.limit_time_v
        if reached and not self.activity_time_reached:
            logger.info('ActivityShikigami task time limit reached; stop before next action')
        self.activity_time_reached = self.activity_time_reached or reached
        return self.activity_time_reached

    def prepare_next_action(self, action_type: str) -> bool:
        """下一次骰子/战斗/行动的唯一软停止与随机休眠节点。"""
        limit = self.action_limit(action_type)
        if limit <= 0 or self.action_count[action_type] >= limit:
            logger.info(
                f'Activity action {action_type} count limit reached: '
                f'{self.action_count[action_type]}/{limit}'
            )
            return False
        if self.time_limit_reached():
            return False

        if self.conf.general_config.random_sleep:
            random_sleep(probability=0.2)
            if self.time_limit_reached():
                return False

        self.current_action_type = action_type
        return True

    def record_action(self, action_type: str):
        self.action_count[action_type] += 1
        logger.info(
            f'Activity action {action_type} count: '
            f'{self.action_count[action_type]}/{self.action_limit(action_type)}'
        )

    def switch_soul_for(
        self,
        action_type: str,
        enter_button: RuleImage,
        *,
        return_page=None,
        exit_records: bool = False,
    ):
        """按六种战斗字段切换一次御魂。"""
        if self.switched_soul[action_type]:
            return
        conf = self.conf.switch_soul_config
        enable_number = getattr(conf, f'enable_switch_{action_type}')
        enable_name = getattr(conf, f'enable_switch_{action_type}_by_name')
        self.switched_soul[action_type] = True
        if not enable_number and not enable_name:
            return

        conf.validate_switch_soul()
        logger.hr(f'Start switch soul: {action_type}', 2)
        self.ui_click(enter_button, stop=self.I_CHECK_RECORDS, interval=1)
        if enable_name:
            group, team = getattr(conf, f'{action_type}_group_team_name').split(',', 1)
            self.run_switch_soul_by_name(group.strip(), team.strip())
        elif enable_number:
            self.run_switch_soul(getattr(conf, f'{action_type}_group_team'))

        if exit_records:
            self.exit_shikigami_records()
        if return_page is not None:
            self.goto_page(return_page)

    def switch_soul_for_from_courtyard(self, action_type: str):
        """导航进入式神录，按原配置切换指定玩法的御魂预设。"""
        if self.switched_soul[action_type]:
            return
        conf = self.conf.switch_soul_config
        enable_number = getattr(conf, f'enable_switch_{action_type}')
        enable_name = getattr(conf, f'enable_switch_{action_type}_by_name')
        self.switched_soul[action_type] = True
        if not enable_number and not enable_name:
            return

        conf.validate_switch_soul()
        logger.hr(f'Start switch soul via navigator: {action_type}', 2)
        self.goto_page(pages.page_shikigami_records)
        if enable_name:
            group, team = getattr(conf, f'{action_type}_group_team_name').split(',', 1)
            self.run_switch_soul_by_name(group.strip(), team.strip())
        elif enable_number:
            self.run_switch_soul(getattr(conf, f'{action_type}_group_team'))

        self.exit_shikigami_records()

    def finish_activity_task(self):
        self.goto_page(pages.page_main)
        if self.conf.general_config.active_souls_clean:
            self.set_next_run(task='SoulsTidy', success=False, finish=False, target=datetime.now())
        self.set_next_run(task=self.scheduled_task_name, success=True)
        raise TaskEnd
