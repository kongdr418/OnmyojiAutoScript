"""超鬼王：模式、配置迁移、召唤星级复核、高星快速上阵与强力追击。"""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from pydantic import ValidationError

from module.config.config_model import ConfigModel
from tasks.MetaDemon.boss_battle import BossBattle
from tasks.MetaDemon.config import (
    BossType, MetaDemon, MetaDemonConfig, MetaDemonMode, MetaDemonQuickPresetConfig, loadout_group,
)
from tasks.MetaDemon.synthesis import parse_synthesis_material_level


class MetaDemonSettingsChecks(unittest.TestCase):
    def test_modes_are_mutually_exclusive_and_accept_historical_values(self):
        self.assertEqual(MetaDemonConfig().mode, [MetaDemonMode.BOSS_BATTLE])
        self.assertEqual(MetaDemonConfig(mode=['鬼王协助']).mode, [MetaDemonMode.BOSS_ASSIST])
        with self.assertRaises(ValidationError):
            MetaDemonConfig(mode=['鬼王战斗', '鬼王协助'])
        with self.assertRaises(ValidationError):
            MetaDemonConfig(mode=['门票刷取：探索'])
        saved = MetaDemonConfig(mode=['鬼王协助']).model_dump()
        self.assertEqual(MetaDemonConfig.model_validate(saved).mode, [MetaDemonMode.BOSS_ASSIST])

    def test_synthesis_checks_selected_material_before_consuming(self):
        for title, level in [('合成二星裂变之眼', 1), ('合成 六星 裂变之眼', 5), ('未知标题', None)]:
            self.assertEqual(parse_synthesis_material_level(title), level)
        owner = object.__new__(BossBattle)
        owner.device = SimpleNamespace(image=Mock(name='image'), image_frame_id='frame')
        owner.screenshot = Mock()
        owner.appear = Mock(side_effect=lambda rule, **kwargs: rule is owner.I_MD_SYNTHESIS_ONE_STAR)
        owner.appear_then_click = Mock(return_value=True)
        owner.click = Mock(return_value=True)
        with patch.object(owner.O_SYNTHESIS_LEVEL, 'ocr', return_value='合成六星裂变之眼'), \
             patch('tasks.MetaDemon.boss_battle.recognize_ticket_stars', return_value=1), \
             patch('tasks.MetaDemon.boss_battle.time.sleep'):
            owner.do_synthesis(owner.I_MD_SYNTHESIS_ONE_STAR, 3, BossType.ONE_STAR)
        # 标题星级与配置星级不一致时，允许点选材料但绝不点击开始合成。
        self.assertEqual(owner.click.call_count, 3)
        for call in owner.click.call_args_list:
            self.assertIs(call.args[0], owner.I_MD_SYNTHESIS_ONE_STAR)
        owner.appear_then_click.assert_not_called()

    def test_legacy_switch_soul_migrates_low_star_preset_only(self):
        conf = MetaDemon(switch_soul={'enable': True, 'two_stars': '4,2', 'enable_two_stars_preset': True})
        high = conf.battle_config('high_star')
        # 旧版 switch_soul 里的高星预设不再自动启用，避免沿用过期阵容。
        self.assertFalse(high.preset_enable)
        self.assertTrue(conf.low_star_battle_conf.preset_enable)
        self.assertEqual((conf.low_star_battle_conf.preset_group, conf.low_star_battle_conf.preset_team), (4, 2))

    def test_categories_and_independent_presets(self):
        for star, group in ((BossType.ONE_STAR, 'low_star'), (BossType.FOUR_STARS, 'low_star'),
                            (BossType.FIVE_STARS, 'high_star'), (BossType.SIX_STARS, 'high_star'),
                            (None, 'low_star')):
            self.assertEqual(loadout_group(star), group)
        conf = MetaDemon(low_star_battle_conf={'preset_enable': True, 'preset_group': 2},
                         high_star_battle_conf={'preset_enable': True, 'preset_group': 5})
        self.assertEqual(conf.battle_config('low_star').preset_group, 2)
        self.assertEqual(conf.battle_config('high_star').preset_group, 5)

    def test_legacy_souls_and_low_preset_migrate(self):
        conf = MetaDemon(switch_soul={'enable': True, 'two_stars': '4,2', 'enable_two_stars_preset': True})
        souls = conf.switch_soul_config
        self.assertTrue(souls.low_star_enable)
        self.assertEqual(souls.low_star_group_team, '4,2')
        self.assertTrue(conf.low_star_battle_conf.preset_enable)
        self.assertEqual((conf.low_star_battle_conf.preset_group, conf.low_star_battle_conf.preset_team), (4, 2))

    def test_settings_payload_has_flat_groups_and_defaults(self):
        payload = ConfigModel.script_task(SimpleNamespace(meta_demon=MetaDemon()), 'MetaDemon')
        self.assertEqual(set(payload), {'scheduler', 'meta_demon_config', 'general_config', 'switch_soul_config',
                                        'low_star_battle_conf', 'high_star_battle_conf', 'quick_preset_config',
                                        'settings_layout_config', 'assistance_config'})
        for group in payload.values():
            self.assertIsInstance(group, list)
            for field in group:
                self.assertIn('default', field)
        modes = {field['name']: field for field in payload['meta_demon_config']}['mode']
        self.assertEqual(modes['enumEnum'], ['鬼王战斗', '鬼王协助'])

    def test_legacy_powerful_list_migrates_to_high_star_switch(self):
        for old, expected in (('5', True), ('6', True), ('4', False), ('', False), ('4 6', True)):
            with self.subTest(old=old):
                conf = MetaDemon(meta_demon_config={'powerful_list': old})
                self.assertIs(conf.meta_demon_config.high_star_powerful_enable, expected)
                self.assertEqual(conf.meta_demon_config.powerful_count, 1)
                self.assertNotIn('powerful_list', conf.meta_demon_config.model_dump())

    def test_powerful_fire_only_for_high_stars_within_attempt_budget(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(meta_demon_config={'high_star_powerful_enable': True, 'powerful_count': 2})
        owner._current_boss_attempts = 0
        for star, expected in ((BossType.FOUR_STARS, False), (BossType.FIVE_STARS, True),
                               (BossType.SIX_STARS, True), (None, False)):
            owner.cur_boss_type = star
            self.assertIs(owner.enable_powerful_fire, expected)
        owner.cur_boss_type = BossType.SIX_STARS
        owner._current_boss_attempts = 2
        self.assertFalse(owner.enable_powerful_fire)

    def test_named_presets_parse_and_reject_invalid_rules(self):
        conf = MetaDemonQuickPresetConfig(
            preset_groups='高星,高星组,天狗;通用,通用组,其他',
            high_star_custom_preset='高星:大天狗,酒吞童子;通用:ALL')
        self.assertEqual(conf.named_presets(), {'大天狗': ('高星组', '天狗'),
                                                '酒吞童子': ('高星组', '天狗'),
                                                'ALL': ('通用组', '其他')})
        for groups, rules in (('高星,高星组,天狗', '缺失:大天狗'),
                              ('高星,高星组,天狗', '高星'),
                              ('高星,高星组,天狗;高星,二组,队', '高星:ALL'),
                              ('高星,高星组', '高星:ALL')):
            with self.subTest(groups=groups, rules=rules), self.assertRaises(ValueError):
                MetaDemonQuickPresetConfig(preset_groups=groups, high_star_custom_preset=rules).named_presets()

    def test_legacy_custom_preset_format_migrates(self):
        conf = MetaDemonQuickPresetConfig(high_star_custom_preset='大天狗:("高星","天狗");ALL:("通用","其他")')
        self.assertEqual(conf.named_presets(), {'大天狗': ('高星', '天狗'), 'ALL': ('通用', '其他')})

    def test_low_and_high_share_loadout_within_category(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(
            switch_soul_config={'low_star_enable': True, 'low_star_group_team': '2,3'},
            general_config={'high_star_quick_loadout_enable': True},
            preset_config={'high_star_enable': True, 'high_star_group_name': '默认分组', 'high_star_preset_name': '超鬼王'})
        owner.goto_page = Mock()
        owner.run_switch_soul = Mock()
        owner.run_quick_loadout = Mock(return_value=True)
        for star in range(1, 7):
            owner.cur_boss_type = BossType(star)
            self.assertTrue(owner.check_and_switch_soul())
        owner.run_switch_soul.assert_called_once_with('2,3')
        owner.run_quick_loadout.assert_called_once()
        config = owner.run_quick_loadout.call_args.args[0]
        self.assertEqual((config.group_name, config.preset_name), ('默认分组', '超鬼王'))
        self.assertIs(owner.run_quick_loadout.call_args.kwargs['frame'], owner.F_QUICK_LOADOUT_FRAME)
        self.assertFalse(owner.run_quick_loadout.call_args.kwargs['skip_equip_soul'])
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

    def test_quick_loadout_skips_soul_equip_when_configured(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(
            general_config={'high_star_quick_loadout_enable': True},
            switch_soul_config={'high_star_group_team_name': '高星组,天狗'},
            quick_preset_config={'high_star_custom_preset_enable': True,
                                 'preset_groups': '高星,高星组,天狗',
                                 'high_star_custom_preset': '高星:ALL',
                                 'skip_equip_soul': True})
        owner.cur_boss_type = BossType.SIX_STARS
        owner._boss_generation = 1
        owner._read_quick_loadout_stage_name = Mock(return_value='大天狗')
        owner.run_quick_loadout = Mock(return_value=True)
        self.assertTrue(owner.check_and_switch_soul())
        self.assertTrue(owner.run_quick_loadout.call_args.kwargs['skip_equip_soul'])

    def test_custom_high_presets_once_per_new_boss(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(general_config={'high_star_quick_loadout_enable': True},
            quick_preset_config={'high_star_custom_preset_enable': True,
                'preset_groups': '高星,高星组,天狗;通用,通用组,其他',
                'high_star_custom_preset': '高星:大天狗;通用:ALL'})
        owner.cur_boss_type = BossType.FIVE_STARS
        owner._boss_generation = 1
        owner._read_quick_loadout_stage_name = Mock(return_value='大天狗')
        owner.run_quick_loadout = Mock(return_value=True)
        owner.goto_page = Mock()
        self.assertTrue(owner.check_and_switch_soul())
        self.assertTrue(owner.check_and_switch_soul())
        owner.run_quick_loadout.assert_called_once()
        target = owner.run_quick_loadout.call_args.args[0]
        self.assertEqual((target.group_name, target.preset_name), ('高星组', '天狗'))
        owner._boss_generation = 2
        owner._read_quick_loadout_stage_name.return_value = '酒吞童子'
        self.assertTrue(owner.check_and_switch_soul())
        self.assertEqual(owner.run_quick_loadout.call_count, 2)
        target = owner.run_quick_loadout.call_args.args[0]
        self.assertEqual((target.group_name, target.preset_name), ('通用组', '其他'))
        owner.goto_page.assert_not_called()

    def test_numeric_legacy_preset_target_migrates_to_named_default(self):
        conf = MetaDemon(general_config={'high_star_quick_loadout_enable': True},
            quick_preset_config={'high_star_custom_preset_enable': True, 'high_star_custom_preset': 'ALL:(1,1)'})
        self.assertEqual(conf.quick_preset_config.named_presets(), {'ALL': ('默认分组', '队伍1')})
        owner = object.__new__(BossBattle)
        owner.conf = conf
        owner.cur_boss_type = BossType.SIX_STARS
        owner._boss_generation = 1
        owner._read_quick_loadout_stage_name = Mock(return_value='大天狗')
        owner.run_quick_loadout = Mock(return_value=True)
        self.assertTrue(owner.check_and_switch_soul())
        target = owner.run_quick_loadout.call_args.args[0]
        self.assertEqual((target.group_name, target.preset_name), ('默认分组', '队伍1'))

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

    def test_ticket_star_recheck_rejects_wrong_star_and_accepts_candidate(self):
        owner = object.__new__(BossBattle)
        owner.device = SimpleNamespace(image=Mock(name='image'), image_frame_id='frame')
        owner.appear = Mock(return_value=True)
        ticket = owner.I_MD_SIX_STARS
        with patch('tasks.MetaDemon.boss_battle.recognize_ticket_stars', return_value=5), \
             patch.object(type(ticket), 'match_all', return_value=[]):
            self.assertFalse(owner.ticket_matches_star(ticket, BossType.SIX_STARS))
        # 另一个候选区域通过复核时必须采用该区域，而不能沿用模板命中的区域。
        with patch('tasks.MetaDemon.boss_battle.recognize_ticket_stars',
                   side_effect=lambda image, area, **kwargs: 6 if tuple(area)[:2] == (700, 300) else 5), \
             patch.object(type(ticket), 'match_all', return_value=[(0.9, 700, 300, 66, 69)]):
            ticket.roi_front = [856, 316, 66, 69]
            self.assertTrue(owner.ticket_matches_star(ticket, BossType.SIX_STARS))
            self.assertEqual(list(ticket.roi_front[:2]), [700, 300])

    def test_recognize_ticket_stars_counts_illuminated_stars(self):
        from tasks.MetaDemon.ticket_stars import recognize_ticket_stars
        self.assertIsNone(recognize_ticket_stars(None, (0, 0, 66, 69)))

        def ticket(stars):
            # 识别先缩放到 72x72，这里直接按缩放后的坐标画点亮的星位。
            card = np.zeros((72, 72, 3), dtype=np.uint8)
            for index, center in enumerate((11, 21, 31, 41, 51, 61)):
                if index < stars:
                    card[58:67, center - 3:center + 3] = (255, 160, 20)
            return card

        for stars in (1, 3, 6):
            self.assertEqual(recognize_ticket_stars(ticket(stars), (0, 0, 72, 72)), stars)
        self.assertIsNone(recognize_ticket_stars(ticket(0), (0, 0, 72, 72)))
        # 点亮顺序不连续时无法判定星级，必须返回 None 而不是猜测。
        broken = ticket(3)
        broken[58:67, 51:57] = (255, 160, 20)
        broken[58:67, 21:27] = 0
        self.assertIsNone(recognize_ticket_stars(broken, (0, 0, 72, 72)))

    def test_disabled_souls_do_not_navigate(self):
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon()
        owner.cur_boss_type = BossType.ONE_STAR
        owner.goto_page = Mock()
        self.assertTrue(owner.check_and_switch_soul())
        owner.goto_page.assert_not_called()

    def _summon_owner(self, frames, star=BossType.TWO_STARS):
        from copy import deepcopy
        owner = object.__new__(BossBattle)
        owner.conf = MetaDemon(meta_demon_config={'fire_sequence': '2 1'})
        owner.State = SimpleNamespace(done=False)
        owner.cur_boss_type = None
        owner.current_count = 7
        owner._boss_generation = 0
        owner._current_boss_attempts = 0
        owner.device = SimpleNamespace(image=Mock(name='image'), image_frame_id='frame')
        # 真实星级识别依赖截图内容，这里固定为目标星级，只验证调用顺序。
        recognize = patch('tasks.MetaDemon.boss_battle.recognize_ticket_stars',
                          side_effect=lambda image, area, **kwargs: int(star))
        recognize.start()
        self.addCleanup(recognize.stop)
        names = ('I_MD_FIRE', 'I_MD_GET_BOSS', 'I_MD_TICKET_EMPTY', 'I_MD_SWITCH_TICKET',
                 'I_MD_CLOSE_POPUP', 'I_MD_CHECK_SELECTED', 'I_MD_ONE_STAR', 'I_MD_TWO_STARS',
                 'I_MD_THREE_STARS', 'I_MD_FOUR_STARS', 'I_MD_FIVE_STARS', 'I_MD_SIX_STARS')
        for name in names:
            setattr(owner, name, deepcopy(getattr(owner, name)))
        target_ticket = {BossType.TWO_STARS: owner.I_MD_TWO_STARS,
                         BossType.SIX_STARS: owner.I_MD_SIX_STARS}[star]
        state = {'index': -1, 'time': 0.0}
        clicks = []

        def screenshot():
            state['index'] += 1
            state['time'] += 1

        def appear(rule, **kwargs):
            frame = frames[min(state['index'], len(frames) - 1)]
            if rule is owner.I_MD_FIRE:
                return frame.get('fire', False)
            if rule is owner.I_MD_GET_BOSS:
                return frame.get('panel', False)
            if rule is owner.I_MD_SWITCH_TICKET:
                return not frame.get('panel', False) and not frame.get('fire', False)
            if rule is target_ticket:
                return frame.get('panel', False)
            if rule is owner.I_MD_CHECK_SELECTED and frame.get('panel'):
                x = target_ticket.roi_front[0] if frame.get('selected') else 360
                rule.roi_front = (x, 280, 50, 22)
                return True
            return False

        def click(rule, **kwargs):
            if not appear(rule):
                return False
            clicks.append((state['index'], rule))
            return True

        owner.screenshot = Mock(side_effect=screenshot)
        owner.appear = Mock(side_effect=appear)
        owner.appear_then_click = Mock(side_effect=click)
        owner.click = Mock(side_effect=click)
        return owner, state, clicks

    def test_summon_sequence_override_only_uses_six_without_changing_config(self):
        owner, state, clicks = self._summon_owner([
            {'panel': True, 'selected': True}, {'fire': True}], star=BossType.SIX_STARS)
        original = owner.conf.meta_demon_config.fire_sequence
        with patch('tasks.MetaDemon.boss_battle.time.monotonic', side_effect=lambda: state['time']):
            self.assertTrue(owner.check_and_switch_ticket(sequence=(BossType.SIX_STARS,)))
        self.assertEqual(owner.cur_boss_type, BossType.SIX_STARS)
        self.assertEqual(owner.conf.meta_demon_config.fire_sequence, original)

    def test_summon_recovers_closed_panel_and_confirms_two_star(self):
        owner, state, clicks = self._summon_owner([
            {'panel': True}, {}, {}, {'panel': True}, {'panel': True, 'selected': True},
            {}, {'fire': True}])
        with patch('tasks.MetaDemon.boss_battle.time.monotonic', side_effect=lambda: state['time']):
            self.assertTrue(owner.check_and_switch_ticket())
        self.assertTrue(any(rule is owner.I_MD_SWITCH_TICKET for _, rule in clicks))
        self.assertEqual([index for index, rule in clicks if rule is owner.I_MD_GET_BOSS], [4])
        self.assertEqual(owner.cur_boss_type, BossType.TWO_STARS)
        self.assertEqual(owner._boss_generation, 1)
        self.assertEqual(owner.current_count, 0)

    def test_summon_retries_unresponsive_confirmation_without_reselecting(self):
        owner, state, clicks = self._summon_owner([
            {'panel': True, 'selected': True}, {'panel': True, 'selected': True}, {'fire': True}])
        with patch('tasks.MetaDemon.boss_battle.time.monotonic', side_effect=lambda: state['time']):
            self.assertTrue(owner.check_and_switch_ticket())
        self.assertEqual(sum(rule is owner.I_MD_GET_BOSS for _, rule in clicks), 2)
        self.assertFalse(any(rule is owner.I_MD_TWO_STARS for _, rule in clicks))

    def test_summon_does_not_treat_missed_template_as_exhausted_tickets(self):
        owner, state, clicks = self._summon_owner([{'panel': True}])
        with patch('tasks.MetaDemon.boss_battle.time.monotonic', side_effect=lambda: state['time']):
            self.assertFalse(owner.check_and_switch_ticket())
        self.assertFalse(owner.State.done)
        self.assertFalse(any(rule is owner.I_MD_GET_BOSS for _, rule in clicks))
        self.assertIsNone(owner.cur_boss_type)
        self.assertEqual(owner._boss_generation, 0)
        self.assertLessEqual(owner.screenshot.call_count, 25)


if __name__ == '__main__':
    unittest.main()
