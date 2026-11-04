"""MetaDemon loadout categories, settings payload and ticket-runner mapping."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from module.config.config_model import ConfigModel
from tasks.MetaDemon.config import BossType, ExplorationTarget, MetaDemon, MetaDemonMode, loadout_group
from tasks.MetaDemon.boss_battle import BossBattle
from tasks.MetaDemon.ticket_farming import configure_ticket_farming, ExplorationTicketTask
from tasks.EvoZone.config import EvoZone
from tasks.Exploration.config import AutoRotate, Exploration, UpType


class MetaDemonSettingsChecks(unittest.TestCase):
    def test_exploration_label_migrates(self):
        conf = MetaDemon(meta_demon_config={'mode': ['鬼王战斗', '门票刷取：探索绘卷']})
        self.assertEqual(conf.meta_demon_config.mode, [MetaDemonMode.EXPLORATION, MetaDemonMode.BOSS_BATTLE])
        self.assertEqual(MetaDemonMode.EXPLORATION.value, '门票刷取：探索')

    def test_categories_and_independent_presets(self):
        self.assertEqual([loadout_group(BossType(i)) for i in range(1, 7)], ['low_star']*4+['high_star']*2)
        conf = MetaDemon(preset_config={'low_star_enable': True, 'low_star_group': 2, 'low_star_team': 3,
            'ticket_farming_enable': True, 'ticket_farming_group': 4, 'ticket_farming_team': 2})
        low = conf.battle_config('low_star')
        ticket = conf.battle_config('ticket_farming')
        self.assertEqual((low.preset_group, low.preset_team), (2,3))
        self.assertEqual((ticket.preset_group, ticket.preset_team), (4,2))
        self.assertFalse(conf.battle_config('high_star').preset_enable)

    def test_legacy_souls_and_low_preset_migrate(self):
        conf = MetaDemon(switch_soul={'enable': True, 'one_star': '2,3', 'two_stars': '3,4',
            'five_stars': '活动,高星', 'enable_one_star_preset': True})
        low = conf.switch_soul_config.switch_for('low_star')
        high = conf.switch_soul_config.switch_for('high_star')
        self.assertTrue(low.enable)
        self.assertEqual(low.switch_group_team, '2,3')
        self.assertTrue(high.enable_switch_by_name)
        self.assertEqual((high.group_name, high.team_name), ('活动','高星'))
        self.assertTrue(conf.low_star_battle_conf.preset_enable)
        self.assertNotIn('switch_soul', conf.model_dump())

    def test_common_settings_map_to_ticket_runners(self):
        expected = {ExplorationTarget.ALL: UpType.ALL, ExplorationTarget.COIN: UpType.COIN,
                    ExplorationTarget.EXP: UpType.EXP, ExplorationTarget.DARUMA: UpType.DARUMAA}
        for target, up_type in expected.items():
            meta = MetaDemon(general_config={'exploration_rotate': True,
                'awake_buff_enable': True, 'exploration_target': target},
                switch_soul_config={'ticket_farming_by_name': True, 'ticket_farming_group_team_name': '刷票,探索'},
                preset_config={'ticket_farming_enable': True, 'ticket_farming_group': 3, 'ticket_farming_team': 2})
            awake, exploration = EvoZone(), Exploration()
            configure_ticket_farming(meta, awake, exploration)
            self.assertEqual(exploration.exploration_config.auto_rotate, AutoRotate.yes)
            self.assertEqual(exploration.exploration_config.up_type, up_type)
            self.assertFalse(exploration.scrolls.scrolls_enable)
            self.assertTrue(awake.evo_zone_config.soul_buff_enable)
            for task in (awake, exploration):
                self.assertTrue(task.switch_soul_config.enable_switch_by_name)
                self.assertEqual(task.switch_soul_config.group_name, '刷票')
                self.assertTrue(task.general_battle_config.preset_enable)
                self.assertEqual(task.general_battle_config.preset_group, 3)
            self.assertIsNot(awake.switch_soul_config, exploration.switch_soul_config)

    def test_ticket_rotation_reuses_exploration_and_disables_scrolls(self):
        from tasks.Exploration.script_task import ScriptTask as ExplorationTask
        self.assertIs(ExplorationTicketTask.switch_rotate, ExplorationTask.switch_rotate)
        self.assertIs(ExplorationTicketTask.run_on_exp_settings, ExplorationTask.run_on_exp_settings)
        for enabled in (False, True):
            meta = MetaDemon(general_config={'exploration_rotate': enabled, 'scrolls_enable': True})
            awake, exploration = EvoZone(), Exploration()
            exploration.scrolls.scrolls_enable = True
            configure_ticket_farming(meta, awake, exploration)
            self.assertEqual(exploration.exploration_config.auto_rotate,
                             AutoRotate.yes if enabled else AutoRotate.no)
            self.assertFalse(exploration.scrolls.scrolls_enable)
            self.assertNotIn('scrolls_enable', meta.general_config.model_dump())
        # Even a stale enabled scrolls setting cannot perform ticket OCR or scheduling.
        owner = object.__new__(ExplorationTicketTask)
        owner.O_REALM_RAID_NUMBER1_META = Mock()
        owner.O_REALM_RAID_NUMBER_META = Mock()
        owner.set_next_run = Mock()
        import tasks.Exploration.page as pages
        exploration.scrolls.scrolls_enable = True
        for page in (pages.page_exp_main, pages.page_exp_settings, pages.page_exploration, pages.page_exp_entrance):
            owner.activate_realm_raid(exploration.scrolls, exploration.exploration_config, page)
        owner.O_REALM_RAID_NUMBER1_META.ocr_digit_counter.assert_not_called()
        owner.O_REALM_RAID_NUMBER_META.ocr_digit_counter.assert_not_called()
        owner.set_next_run.assert_not_called()

    def test_settings_payload_has_flat_groups_and_defaults(self):
        payload = ConfigModel.script_task(SimpleNamespace(meta_demon=MetaDemon()), 'MetaDemon')
        self.assertEqual(set(payload), {'scheduler','meta_demon_config','general_config','switch_soul_config','low_star_battle_conf','high_star_battle_conf','ticket_farming_battle_conf','quick_preset_config','settings_layout_config'})
        for group in payload.values():
            self.assertIsInstance(group, list)
            for field in group:
                self.assertIn('default', field)
        common = {field['name']: field for field in payload['general_config']}
        self.assertEqual(common['exploration_target']['enumEnum'], ['全部','金币','经验','达摩'])

    def test_low_and_high_share_loadout_within_category(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(
            switch_soul_config={'low_star_enable': True, 'low_star_group_team': '2,3'},
            general_config={'high_star_quick_loadout_enable': True},
            preset_config={'high_star_enable': True, 'high_star_group_name': '默认分组', 'high_star_preset_name': '超鬼王'})
        owner.goto_page = Mock()
        owner.run_switch_soul = Mock()
        owner.run_quick_loadout = Mock(return_value=True)
        for star in range(1,7):
            owner.cur_boss_type = BossType(star)
            self.assertTrue(owner.check_and_switch_soul())
        owner.run_switch_soul.assert_called_once_with('2,3')
        owner.run_quick_loadout.assert_called_once()
        config = owner.run_quick_loadout.call_args.args[0]
        self.assertEqual((config.group_name, config.preset_name), ('默认分组','超鬼王'))
        self.assertIs(owner.run_quick_loadout.call_args.kwargs['frame'], owner.F_QUICK_LOADOUT_FRAME)
        owner._battle_shared_state = {'meta_demon_low_star': object()}
        owner.cur_boss_type = BossType.ONE_STAR
        self.assertTrue(owner.check_and_switch_soul())
        self.assertNotIn('meta_demon_low_star', owner._battle_shared_state)

    def test_high_star_default_uses_records_and_battle_preset(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(
            switch_soul_config={'high_star_enable': True, 'high_star_group_team': '3,2'},
            high_star_battle_conf={'preset_enable': True, 'preset_group': 4, 'preset_team': 3})
        owner.cur_boss_type = BossType.FIVE_STARS
        owner.goto_page = Mock()
        owner.run_switch_soul = Mock()
        owner.run_quick_loadout = Mock()
        self.assertTrue(owner.check_and_switch_soul())
        owner.run_switch_soul.assert_called_once_with('3,2')
        self.assertEqual(owner.goto_page.call_count, 2)
        owner.run_quick_loadout.assert_not_called()
        self.assertTrue(owner.conf.battle_config('high_star').preset_enable)

    def test_quick_loadout_only_overrides_high_star(self):
        conf = MetaDemon(general_config={'high_star_quick_loadout_enable': True},
            low_star_battle_conf={'preset_enable': True},
            high_star_battle_conf={'preset_enable': True, 'green_enable': True},
            ticket_farming_battle_conf={'preset_enable': True, 'green_enable': True})
        self.assertTrue(conf.battle_config('low_star').preset_enable)
        self.assertFalse(conf.battle_config('high_star').preset_enable)
        self.assertTrue(conf.battle_config('high_star').green_enable)
        self.assertTrue(conf.high_star_battle_conf.preset_enable)
        awake, exploration = EvoZone(), Exploration()
        configure_ticket_farming(conf, awake, exploration)
        for task in (awake, exploration):
            self.assertTrue(task.general_battle_config.preset_enable)
            self.assertTrue(task.general_battle_config.green_enable)

    def test_custom_high_presets_once_per_new_boss(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(general_config={'high_star_quick_loadout_enable': True},
            quick_preset_config={'high_star_custom_preset_enable': True,
                'high_star_custom_preset': '大天狗:("高星","天狗");ALL:("通用","其他")'})
        owner.cur_boss_type = BossType.FIVE_STARS
        owner._boss_generation = 1
        owner._read_quick_loadout_stage_name = Mock(return_value='大天狗')
        owner.run_quick_loadout = Mock(return_value=True)
        owner.goto_page = Mock()
        self.assertTrue(owner.check_and_switch_soul())
        self.assertTrue(owner.check_and_switch_soul())
        owner.run_quick_loadout.assert_called_once()
        target = owner.run_quick_loadout.call_args.args[0]
        self.assertEqual((target.group_name, target.preset_name), ('高星', '天狗'))
        owner._boss_generation = 2
        owner._read_quick_loadout_stage_name.return_value = '酒吞童子'
        self.assertTrue(owner.check_and_switch_soul())
        self.assertEqual(owner.run_quick_loadout.call_count, 2)
        target = owner.run_quick_loadout.call_args.args[0]
        self.assertEqual((target.group_name, target.preset_name), ('通用', '其他'))
        owner.goto_page.assert_not_called()

    def test_numeric_custom_target_rejected_without_deploy(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(general_config={'high_star_quick_loadout_enable': True},
            quick_preset_config={'high_star_custom_preset_enable': True, 'high_star_custom_preset': 'ALL:(1,1)'})
        owner.cur_boss_type = BossType.SIX_STARS
        owner.run_quick_loadout = Mock()
        self.assertFalse(owner.check_and_switch_soul())
        owner.run_quick_loadout.assert_not_called()

    def test_custom_high_presets_ignored_when_quick_disabled(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(quick_preset_config={'high_star_custom_preset_enable': True},
            switch_soul_config={'high_star_enable': True, 'high_star_group_team': '2,3'})
        owner.cur_boss_type = BossType.FIVE_STARS
        owner.goto_page = Mock()
        owner.run_switch_soul = Mock()
        owner.run_quick_loadout = Mock()
        self.assertTrue(owner.check_and_switch_soul())
        owner.run_switch_soul.assert_called_once_with('2,3')
        owner.run_quick_loadout.assert_not_called()

    def test_disabled_souls_do_not_navigate(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon()
        owner.cur_boss_type = BossType.ONE_STAR
        owner.goto_page = Mock()
        self.assertTrue(owner.check_and_switch_soul())
        owner.goto_page.assert_not_called()


class ConfigProxy:
    """Use a real config model without reading or writing user config files."""
    def __init__(self, model):
        self.model = model

    def __getattr__(self, key):
        return getattr(self.model, key)


class MetaDemonRuntimeChecks(unittest.TestCase):
    def test_modes_use_existing_settings_controls(self):
        from tasks.MetaDemon.config import MetaDemonConfig, TicketFarmingMode
        from pydantic import ValidationError
        conf = MetaDemonConfig(mode=[MetaDemonMode.BOSS_BATTLE, MetaDemonMode.AWAKE])
        self.assertEqual(conf.mode, [MetaDemonMode.AWAKE, MetaDemonMode.BOSS_BATTLE])
        self.assertEqual(MetaDemonConfig().mode, [MetaDemonMode.BOSS_BATTLE])
        self.assertEqual(MetaDemonConfig(mode=[]).mode, [])
        with self.assertRaises(ValidationError):
            MetaDemonConfig(mode=[MetaDemonMode.AWAKE, MetaDemonMode.EXPLORATION])
        payload = ConfigModel.script_task(SimpleNamespace(meta_demon=MetaDemon()), 'MetaDemon')
        fields = {row['name']: row for row in payload['meta_demon_config']}
        self.assertEqual(fields['boss_battle_enable']['type'], 'boolean')
        self.assertEqual(fields['ticket_farming_mode']['type'], 'enum')
        self.assertEqual(fields['ticket_farming_mode']['enumEnum'], [item.value for item in TicketFarmingMode])

    def test_high_star_legacy_battle_preset_is_preserved(self):
        conf = MetaDemon(switch_soul={'five_stars': '3,2', 'enable_five_stars_preset': True})
        high = conf.battle_config('high_star')
        self.assertEqual((high.preset_enable, high.preset_group, high.preset_team), (True, 3, 2))

    def test_farming_runs_before_boss(self):
        from tasks.MetaDemon.script_task import ScriptTask
        owner = object.__new__(ScriptTask)
        owner.config = SimpleNamespace(meta_demon=MetaDemon(meta_demon_config={'mode': [MetaDemonMode.BOSS_BATTLE, MetaDemonMode.AWAKE]}))
        events = []
        with patch('tasks.MetaDemon.ticket_farming.run_ticket_farming', side_effect=lambda task, mode: events.append(mode)), \
             patch.object(BossBattle, 'run', side_effect=lambda: events.append('boss')):
            owner.run()
        self.assertEqual(events, [MetaDemonMode.AWAKE, 'boss'])

    def test_ocr_requires_valid_capacity(self):
        from tasks.MetaDemon.ticket_farming import metademon_tickets_full, MetaDemonAssets
        owner = SimpleNamespace(device=SimpleNamespace(image=None))
        for count, expected in [((100, 0, 100), True), ((99, 1, 100), False), ((0, 0, 0), False), ((-1, 101, 100), False)]:
            with self.subTest(count=count), patch.object(MetaDemonAssets.O_METADEMON_PASS, 'ocr_digit_counter', return_value=count):
                self.assertEqual(metademon_tickets_full(owner), expected)

    def test_full_reward_interrupts_before_click(self):
        from tasks.MetaDemon.ticket_farming import AwakeTicketTask, TicketsFull
        owner = object.__new__(AwakeTicketTask)
        owner.click = Mock()
        with patch('tasks.MetaDemon.ticket_farming.metademon_tickets_full', return_value=True):
            with self.assertRaises(TicketsFull):
                owner._handle_reward(Mock(), Mock())
        owner.click.assert_not_called()

    def test_runner_copies_config_and_cleans_buffs_on_full_and_error(self):
        from tasks.MetaDemon.ticket_farming import run_ticket_farming, TicketsFull
        from tasks.EvoZone.config import Layer, UserStatus
        for mode in (MetaDemonMode.AWAKE, MetaDemonMode.EXPLORATION):
            for result in (None, TicketsFull(), RuntimeError('battle failed')):
                with self.subTest(mode=mode, result=type(result).__name__):
                    model = ConfigModel()
                    model.meta_demon.general_config.awake_buff_enable = True
                    model.exploration.exploration_config.buff_exp_50_click = True
                    owner = SimpleNamespace(config=ConfigProxy(model), device=Mock())
                    original = model.model_dump_json()
                    runners = []
                    def factory(config, device):
                        runner = Mock(config=config)
                        runner.run_alone.side_effect = result
                        runner.exec_exp_page.side_effect = result
                        runners.append(runner)
                        return runner
                    with patch('tasks.MetaDemon.ticket_farming.AwakeTicketTask', side_effect=factory), \
                         patch('tasks.MetaDemon.ticket_farming.ExplorationTicketTask', side_effect=factory), \
                         patch.object(ConfigModel, 'save') as save:
                        if isinstance(result, RuntimeError):
                            with self.assertRaises(RuntimeError):
                                run_ticket_farming(owner, mode)
                        else:
                            self.assertEqual(run_ticket_farming(owner, mode), isinstance(result, TicketsFull))
                        save.assert_not_called()
                    runner = runners[0]
                    self.assertEqual(model.model_dump_json(), original)
                    self.assertIsNot(runner.config.model, model)
                    self.assertTrue(runner._ticket_farming_complete)
                    if mode == MetaDemonMode.AWAKE:
                        settings = runner.config.evo_zone.evo_zone_config
                        self.assertEqual((settings.layer, settings.user_status), (Layer.SEVEN, UserStatus.ALONE))
                        runner.awake.assert_any_call(is_open=True)
                        runner.awake.assert_any_call(is_open=False)
                    else:
                        runner.exp_50.assert_called_once_with(is_open=False)
                    runner.close_buff.assert_called()

    def test_page_hooks_and_boss_state_are_session_local(self):
        from tasks.MetaDemon.script_task import ScriptTask
        from tasks.MetaDemon.ticket_farming import AwakeTicketTask
        from tasks.GameUi.default_pages import page_reward, page_main
        from tasks.MetaDemon import page as pages
        config = ConfigProxy(ConfigModel())
        with patch('tasks.base_task.BaseTask.check_costume'):
            first = ScriptTask(config, Mock())
            second = ScriptTask(config, Mock())
            ticket = AwakeTicketTask(config, Mock())
        original = list(page_reward.on_enter_success)
        ticket.install_ticket_reward_hook()
        self.assertEqual(page_reward.on_enter_success, original)
        self.assertEqual(len(ticket.navigator.resolve_page(page_reward).on_enter_success), len(original) + 1)
        first.before_run()
        first.State.done = True
        second.before_run()
        self.assertFalse(second.State.done)
        self.assertIsNot(first.State, second.State)
        self.assertTrue(any(edge.destination == pages.page_meta_demon for edge in page_main.transitions))

    def test_synthesis_checks_selected_material_before_consuming(self):
        from tasks.MetaDemon.synthesis import parse_synthesis_material_level
        for title, level in [('合成二星裂变之眼', 1), ('合成 六星 裂变之眼', 5), ('未知标题', None)]:
            self.assertEqual(parse_synthesis_material_level(title), level)
        owner = object.__new__(BossBattle)
        owner.device = SimpleNamespace(image=None)
        owner.screenshot = Mock()
        owner.appear = Mock(side_effect=lambda rule, **kwargs: rule is owner.I_MD_SYNTHESIS_ONE_STAR)
        owner.appear_then_click = Mock(return_value=True)
        with patch.object(owner.O_SYNTHESIS_LEVEL, 'ocr', return_value='合成六星裂变之眼'), \
             patch('tasks.MetaDemon.boss_battle.time.sleep'):
            owner.do_synthesis(owner.I_MD_SYNTHESIS_ONE_STAR, 3, BossType.ONE_STAR)
        self.assertEqual(owner.appear_then_click.call_count, 3)
        for call in owner.appear_then_click.call_args_list:
            self.assertIs(call.args[0], owner.I_MD_SYNTHESIS_ONE_STAR)


if __name__ == '__main__':
    unittest.main()
