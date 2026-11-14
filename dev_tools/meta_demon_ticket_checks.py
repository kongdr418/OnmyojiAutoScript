"""超鬼王门票刷取（独立任务）：配置、旧配置迁移、满票判定与协同。"""
from types import SimpleNamespace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tasks.MetaDemonTicket.config import (
    ExplorationTarget, MetaDemonTicket, TicketMode, split_legacy_meta_demon,
)
from tasks.MetaDemonTicket.ticket_farming import (
    TicketPartnerFinished, TicketsFull, metademon_tickets_full, run_ticket_farming,
    ticket_farming_config, tickets_full_before_farming,
    TicketCooperation, TicketRewardMixin,
)
from module.cooperation import Cooperation
from module.exception import GamePageUnknownError
from pydantic import ValidationError
from tasks.Component.GeneralInvite.config_invite import InviteConfig
from tasks.Exploration.config import AutoRotate, UpType, UserStatus as ExplorationUserStatus
from tasks.EvoZone.config import KirinType, Layer
from tasks.MetaDemon.assets import MetaDemonAssets
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets


class TicketConfigChecks(unittest.TestCase):
    def test_ticket_invite_accepts_two_profiles_with_multiline_ui(self):
        for value in ('first\nsecond', 'first,second', 'first；second', 'first\nfirst\nsecond'):
            conf = MetaDemonTicket(invite_config={'cooperation_script': value})
            self.assertEqual(conf.invite_config.cooperation_script, 'first\nsecond')
            schema = conf.invite_config.model_json_schema()
            self.assertEqual(schema['properties']['cooperation_script']['type'], 'multi_line')
        for value in ('first\nsecond\nthird', '../profile'):
            with self.assertRaises(ValidationError):
                MetaDemonTicket(invite_config={'cooperation_script': value})
        with self.assertRaises(ValidationError):
            InviteConfig(cooperation_script='first\nsecond')

    def test_defaults_and_single_mode(self):
        conf = MetaDemonTicket()
        self.assertEqual(conf.general_config.mode, TicketMode.AWAKE)
        self.assertEqual(conf.general_config.user_status, ExplorationUserStatus.ALONE)
        self.assertEqual(conf.general_config.awake_layer, Layer.SEVEN)
        self.assertEqual(conf.general_config.awake_kirin_type, KirinType.LIGHTNINGKIRIN)
        self.assertEqual(conf.general_config.exploration_target, ExplorationTarget.ALL)
        self.assertFalse(conf.scheduler.enable)
        self.assertEqual(conf.general_config.limit_time_v.total_seconds(), 45 * 60)

    def test_legacy_meta_demon_tickets_migrate_into_the_task(self):
        legacy = {
            'meta_demon': {
                'scheduler': {'enable': True, 'next_run': '2026-01-01 00:00:00', 'wait_interval': '00 01:40:00'},
                'meta_demon_config': {'mode': ['门票刷取：探索绘卷', '鬼王战斗'], 'limit_time': '01:00:00'},
                'general_config': {'exploration_rotate': True, 'awake_buff_enable': True},
                'switch_soul_config': {'ticket_farming_enable': True, 'ticket_farming_group_team': '2,3',
                                       'ticket_farming_by_name': False, 'ticket_farming_group_team_name': ''},
                'ticket_farming_battle_conf': {'preset_enable': True, 'preset_group': 3},
            }
        }
        data = split_legacy_meta_demon(legacy)
        ticket = data['meta_demon_ticket']
        self.assertEqual(ticket['general_config']['mode'], '门票刷取：探索')
        self.assertEqual(ticket['general_config']['limit_time'], '01:00:00')
        self.assertTrue(ticket['general_config']['exploration_rotate'])
        self.assertTrue(ticket['general_config']['awake_buff_enable'])
        self.assertTrue(ticket['scheduler']['enable'])
        self.assertNotIn('wait_interval', ticket['scheduler'])
        self.assertEqual(ticket['switch_soul_config']['switch_group_team'], '2,3')
        self.assertTrue(ticket['switch_soul_config']['enable'])
        self.assertEqual(ticket['general_battle_config']['preset_group'], 3)
        # 刷票模式从超鬼王里被摘掉，其它设置保留。
        self.assertEqual(data['meta_demon']['meta_demon_config']['mode'], ['鬼王战斗'])
        # 迁移结果必须能被新模型直接加载。
        conf = MetaDemonTicket.model_validate(ticket)
        self.assertEqual(conf.general_config.mode, TicketMode.EXPLORATION)

    def test_legacy_without_ticket_settings_is_untouched(self):
        legacy = {'meta_demon': {'meta_demon_config': {'mode': ['鬼王战斗']}}}
        self.assertIs(split_legacy_meta_demon(legacy), legacy)

    def test_ticket_only_config_disables_meta_demon_scheduler(self):
        legacy = {'meta_demon': {'scheduler': {'enable': True},
                                 'meta_demon_config': {'mode': ['门票刷取：觉醒七层']}}}
        data = split_legacy_meta_demon(legacy)
        self.assertFalse(data['meta_demon']['scheduler']['enable'])
        self.assertEqual(data['meta_demon']['meta_demon_config']['mode'], ['鬼王战斗'])
        self.assertEqual(data['meta_demon_ticket']['general_config']['mode'], '门票刷取：觉醒')


class TicketFarmingChecks(unittest.TestCase):
    def meta(self, **general):
        return MetaDemonTicket(general_config={
            'mode': '门票刷取：觉醒', 'user_status': 'alone', **general})

    def test_awake_config_maps_to_evozone_with_invite(self):
        meta = self.meta(awake_layer=Layer.NINE.value, awake_kirin_type=KirinType.WATERKIRIN.value,
                         awake_buff_enable=True, exploration_rotate=False)
        task = ticket_farming_config(meta, TicketMode.AWAKE)
        self.assertEqual(task.evo_zone_config.layer, Layer.NINE)
        self.assertEqual(task.evo_zone_config.kirin_type, KirinType.WATERKIRIN)
        self.assertTrue(task.evo_zone_config.soul_buff_enable)
        self.assertEqual(task.evo_zone_config.limit_count, 2 ** 31 - 1)
        self.assertIs(task.invite_config, meta.invite_config)
        self.assertEqual(task.switch_soul_config.model_dump(), meta.switch_soul_config.model_dump())

    def test_exploration_config_maps_targets_and_disables_team_lock(self):
        meta = self.meta(mode='门票刷取：探索', exploration_target='金币', exploration_rotate=True,
                         general_battle_config={'lock_team_enable': True})
        task = ticket_farming_config(meta, TicketMode.EXPLORATION)
        self.assertEqual(task.exploration_config.up_type, UpType.COIN)
        self.assertEqual(task.exploration_config.auto_rotate, AutoRotate.yes)
        self.assertFalse(task.general_battle_config.lock_team_enable)
        self.assertIs(task.invite_config, meta.invite_config)

    def test_ticket_ocr_is_skipped_without_red_daruma_reward(self):
        owner = SimpleNamespace(appear=Mock(return_value=False), device=SimpleNamespace(image=object()))
        with patch.object(GeneralBattleAssets.O_METADEMON_PASS, 'ocr_digit_counter') as ocr:
            self.assertFalse(metademon_tickets_full(owner))
        ocr.assert_not_called()

    def test_ticket_counter_requires_valid_numerator_and_total(self):
        # 计数越界（112/50 这类误读）必须当作识别错误，不能判定门票已满。
        cases = {
            (1000, 0, 1000): True,    # 正好满票
            (999, 1, 1000): False,    # 未满
            (1001, -1, 1000): False,  # 超过上限 = 误读
            (112, -62, 50): False,    # 真实案例：112/50 误读
            (-1, 0, 0): False,        # 未识别
        }
        for values, expected in cases.items():
            with self.subTest(values=values):
                owner = SimpleNamespace(appear=Mock(return_value=True), device=SimpleNamespace(image=object()))
                with patch.object(GeneralBattleAssets.O_METADEMON_PASS, 'ocr_digit_counter',
                                  return_value=values):
                    self.assertEqual(metademon_tickets_full(owner), expected)

    def test_precheck_retries_invalid_counter_and_rejects_wrong_page(self):
        owner = SimpleNamespace(goto_page=Mock(), screenshot=Mock(), appear=Mock(return_value=True),
                                device=SimpleNamespace(image=object()))
        with patch.object(MetaDemonAssets.O_METADEMON_PASS_EXP, 'ocr_digit_counter',
                          side_effect=[(0, 0, 0), (-1, 51, 50), (49, 1, 50)]) as ocr:
            self.assertFalse(tickets_full_before_farming(owner))
            self.assertEqual(ocr.call_count, 3)
        owner.appear.return_value = False
        with patch.object(MetaDemonAssets.O_METADEMON_PASS_EXP, 'ocr_digit_counter') as ocr:
            self.assertFalse(tickets_full_before_farming(owner))
            ocr.assert_not_called()

    def _owner(self, mode='门票刷取：觉醒', **general):
        meta = MetaDemonTicket(general_config={'mode': mode, **general})
        return SimpleNamespace(config=SimpleNamespace(meta_demon_ticket=meta, cooperation_runtime=None),
                               device=SimpleNamespace(image=object()), goto_page=Mock(), screenshot=Mock(),
                               appear=Mock(return_value=True))

    def test_full_tickets_skip_before_starting_runner(self):
        owner = self._owner()
        with patch('tasks.MetaDemonTicket.ticket_farming.tickets_full_before_farming', return_value=True), \
             patch('tasks.MetaDemonTicket.ticket_farming.AwakeTicketTask') as runner:
            self.assertTrue(run_ticket_farming(owner, TicketMode.AWAKE))
        runner.assert_not_called()

    def test_full_tickets_interrupt_runner_and_report_success(self):
        owner = self._owner()
        runner = Mock()
        runner.run_alone.side_effect = TicketsFull
        with patch('tasks.MetaDemonTicket.ticket_farming.tickets_full_before_farming', return_value=False), \
             patch('tasks.MetaDemonTicket.ticket_farming.AwakeTicketTask', return_value=runner):
            self.assertTrue(run_ticket_farming(owner, TicketMode.AWAKE))
        runner.install_ticket_reward_hook.assert_called_once()
        self.assertTrue(runner._ticket_farming_complete)

    def test_partner_finished_ends_the_run_without_success(self):
        owner = self._owner()
        runner = Mock()
        runner.exec_exp_page.side_effect = TicketPartnerFinished
        with patch('tasks.MetaDemonTicket.ticket_farming.tickets_full_before_farming', return_value=False), \
             patch('tasks.MetaDemonTicket.ticket_farming.ExplorationTicketTask', return_value=runner):
            self.assertFalse(run_ticket_farming(owner, TicketMode.EXPLORATION))
        self.assertTrue(runner._ticket_farming_complete)

    def test_mismatched_cooperation_pair_is_skipped(self):
        runtime = Mock()
        runtime.peers = {}
        runtime.matches_meta_demon_tickets.return_value = False
        meta = MetaDemonTicket(general_config={'mode': '门票刷取：觉醒', 'user_status': 'leader'},
                               invite_config={'cooperation_script': 'peer'})
        owner = SimpleNamespace(config=SimpleNamespace(meta_demon_ticket=meta, cooperation_runtime=runtime),
                                device=SimpleNamespace(image=object()), goto_page=Mock(), screenshot=Mock(),
                                appear=Mock(return_value=True))
        with patch('tasks.MetaDemonTicket.ticket_farming.tickets_full_before_farming') as precheck:
            self.assertFalse(run_ticket_farming(owner, TicketMode.AWAKE))
        precheck.assert_not_called()

    def test_leader_and_member_use_invite_flows(self):
        for role, method in (('leader', 'run_leader'), ('member', 'run_member'), ('alone', 'run_alone')):
            with self.subTest(role=role):
                owner = self._owner(mode='门票刷取：觉醒', user_status=role)
                runner = Mock()
                with patch('tasks.MetaDemonTicket.ticket_farming.tickets_full_before_farming', return_value=False), \
                     patch('tasks.MetaDemonTicket.ticket_farming.AwakeTicketTask', return_value=runner):
                    self.assertFalse(run_ticket_farming(owner, TicketMode.AWAKE))
                getattr(runner, method).assert_called_once()


class TicketRewardExitChecks(unittest.TestCase):
    def runner(self, complete=True):
        class Frames:
            def screenshot(self):
                self.frames += 1
                return self.frames

        class Runner(TicketRewardMixin, Frames):
            pass

        runner = Runner()
        runner.frames = 0
        runner.reward_visible = True
        runner._ticket_farming_complete = complete
        runner._ticket_link = Mock()
        runner._battle_context = None
        runner.match_page_once = Mock(side_effect=lambda page: runner.reward_visible)
        runner.appear_then_click = Mock(return_value=False)
        runner.click = Mock()
        return runner

    def test_swallowed_reward_click_is_retried_until_page_closes(self):
        runner = self.runner()
        def click(*args, **kwargs):
            if runner.click.call_count == 2:
                runner.reward_visible = False
            return runner.click.call_count == 2
        runner.click.side_effect = click
        self.assertEqual(runner.screenshot(), 3)
        self.assertEqual(runner.click.call_count, 2)
        runner._ticket_link.poll.assert_not_called()

    def test_active_farming_leaves_reward_handling_to_battle_loop(self):
        runner = self.runner(complete=False)
        self.assertEqual(runner.screenshot(), 1)
        runner.click.assert_not_called()
        runner.match_page_once.assert_not_called()
        runner._ticket_link.poll.assert_called_once()

    def test_stuck_reward_exit_has_a_bounded_retry(self):
        runner = self.runner()
        with patch('tasks.MetaDemonTicket.ticket_farming.time.monotonic', side_effect=[0, 0, 31]):
            with self.assertRaisesRegex(GamePageUnknownError, '奖励页面持续未关闭'):
                runner.screenshot()
        self.assertEqual(runner.click.call_count, 1)

    def test_final_reward_cleanup_runs_before_return_navigation_for_every_role(self):
        for role, method in (('leader', 'run_leader'), ('member', 'run_member'), ('alone', 'run_alone')):
            with self.subTest(role=role):
                settings = MetaDemonTicket(general_config={'user_status': role})
                owner = SimpleNamespace(config=SimpleNamespace(meta_demon_ticket=settings, cooperation_runtime=None),
                                        device=SimpleNamespace())
                runner = Mock()
                runner._config = ticket_farming_config(settings, TicketMode.AWAKE)
                getattr(runner, method).side_effect = TicketsFull
                order = []
                def screenshot():
                    self.assertTrue(runner._ticket_farming_complete)
                    order.append('reward_cleanup')
                runner.screenshot.side_effect = screenshot
                runner.goto_page.side_effect = lambda *args, **kwargs: order.append('return_main')
                with patch('tasks.MetaDemonTicket.ticket_farming.tickets_full_before_farming', return_value=False), \
                        patch('tasks.MetaDemonTicket.ticket_farming.AwakeTicketTask', return_value=runner):
                    self.assertTrue(run_ticket_farming(owner, TicketMode.AWAKE))
                self.assertEqual(order, ['reward_cleanup'] + (['return_main'] if role != 'alone' else []))


class TicketGroupCompletionChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'config').mkdir()
        self.group = ('leader', 'member1', 'member2')
        self.runtimes = {}
        for name in self.group:
            role = 'leader' if name == 'leader' else 'member'
            peer = 'member1\nmember2' if role == 'leader' else 'leader'
            settings = MetaDemonTicket(scheduler={'enable': True}, general_config={'user_status': role},
                                      invite_config={'cooperation_script': peer})
            (self.root / 'config' / f'{name}.json').write_text(
                json.dumps({'meta_demon_ticket': settings.model_dump(mode='json')}, ensure_ascii=False),
                encoding='utf-8')
            runtime = Cooperation(name, root=self.root)
            runtime.publish('MetaDemonTicket')
            self.runtimes[name] = runtime

    def links(self, full=(False, False, False)):
        links = [TicketCooperation(self.runtimes[name], self.group, value)
                 for name, value in zip(self.group, full)]
        for member in links[1:]:
            member.poll()
        return links

    def test_leader_requires_everyone_full_and_broadcasts_completion(self):
        leader, first, second = self.links((True, True, False))
        leader.poll()
        first.poll()
        self.assertFalse(leader.complete)
        second.set_full()
        with self.assertRaises(TicketsFull):
            leader.poll()
        for member in (first, second):
            with self.assertRaises(TicketsFull):
                member.poll()

    def test_two_person_cooperation_uses_the_same_completion_flow(self):
        group = self.group[:2]
        leader = TicketCooperation(self.runtimes['leader'], group, True)
        member = TicketCooperation(self.runtimes['member1'], group, True)
        member.poll()
        with self.assertRaises(TicketsFull):
            leader.poll()
        with self.assertRaises(TicketsFull):
            member.poll()

    def test_member_exit_is_relayed_by_leader(self):
        leader, first, second = self.links()
        first.finish(False)
        with self.assertRaises(TicketPartnerFinished):
            leader.poll()
        leader.finish(False)
        with self.assertRaises(TicketPartnerFinished):
            second.poll()

    def test_missing_member_heartbeat_stops_group_after_startup_grace(self):
        leader, first, second = self.links()
        with self.runtimes['leader'].connect() as db:
            db.execute('DELETE FROM runners WHERE name=?', ('member1',))
        leader.poll()
        leader.started -= 31
        with self.assertRaises(TicketPartnerFinished):
            leader.poll()
        leader.finish(False)
        with self.assertRaises(TicketPartnerFinished):
            second.poll()

    def test_old_full_states_are_ignored_after_leader_restart(self):
        old_leader, first, second = self.links((True, True, True))
        with self.assertRaises(TicketsFull):
            old_leader.poll()
        leader = TicketCooperation(self.runtimes['leader'], self.group, True)
        self.assertNotEqual(leader.session, old_leader.session)
        leader.poll()
        first.poll()
        leader.poll()
        second.poll()
        with self.assertRaises(TicketsFull):
            leader.poll()

    def test_late_starting_member_ignores_completed_previous_session(self):
        leader, first, second = self.links((True, True, True))
        with self.assertRaises(TicketsFull):
            leader.poll()
        leader.finish(True)
        restarted = TicketCooperation(self.runtimes['member1'], self.group, False)
        restarted.poll()
        self.assertIsNone(restarted.session)
        next_leader = TicketCooperation(self.runtimes['leader'], self.group, False)
        restarted.poll()
        self.assertEqual(restarted.session, next_leader.session)
        next_leader.poll()

    def test_member_can_start_before_leader_publishes_a_session(self):
        member = TicketCooperation(self.runtimes['member1'], self.group, True)
        member.poll()
        self.assertIsNone(member.session)
        leader = TicketCooperation(self.runtimes['leader'], self.group, False)
        member.poll()
        self.assertEqual(member.session, leader.session)
        state = self.runtimes['leader'].peer_battle_state('MetaDemonTicket', 'member1')
        self.assertTrue(state['payload']['full'])

    def test_failed_group_preparation_publishes_stop_to_both_members(self):
        runtime = self.runtimes['leader']
        settings = MetaDemonTicket.model_validate(runtime.read('leader')['meta_demon_ticket'])
        owner = SimpleNamespace(config=SimpleNamespace(meta_demon_ticket=settings, cooperation_runtime=runtime),
                                device=SimpleNamespace(image=object()))
        runner = Mock()
        with patch('tasks.MetaDemonTicket.ticket_farming.tickets_full_before_farming', return_value=False), \
                patch('tasks.MetaDemonTicket.ticket_farming.AwakeTicketTask', return_value=runner), \
                patch.object(runtime, 'prepare', return_value=False):
            self.assertFalse(run_ticket_farming(owner, TicketMode.AWAKE))
        runner.run_leader.assert_not_called()
        for name in ('member1', 'member2'):
            state = self.runtimes[name].peer_battle_state('MetaDemonTicket', 'leader')
            self.assertTrue(state['payload']['finished'])
            self.assertFalse(state['payload']['complete'])

    def test_starting_task_clears_only_its_previous_outgoing_state(self):
        leader = self.runtimes['leader']
        leader.publish_battle_state('MetaDemonTicket', 'member1', 'old', {'full': True})
        leader.publish_battle_state('MetaDemonAssistance', 'member1', 'keep', {'full': True})
        leader.publish('')
        leader.publish('MetaDemonTicket')
        member = self.runtimes['member1']
        self.assertIsNone(member.peer_battle_state('MetaDemonTicket', 'leader'))
        self.assertEqual(member.peer_battle_state('MetaDemonAssistance', 'leader')['session'], 'keep')

    def test_completion_waits_until_current_battle_has_finished(self):
        class Frames:
            def screenshot(self):
                return 'frame'

        class Runner(TicketRewardMixin, Frames):
            pass

        runner = Runner()
        runner._ticket_farming_complete = False
        runner._battle_context = object()
        runner._ticket_link = Mock()
        runner._ticket_link.poll.side_effect = TicketsFull
        self.assertEqual(runner.screenshot(), 'frame')
        runner._ticket_link.poll.assert_not_called()
        runner._battle_context = None
        with self.assertRaises(TicketsFull):
            runner.screenshot()

    def test_pre_filled_leader_stays_until_both_members_confirm_full(self):
        runtime = self.runtimes['leader']
        settings = MetaDemonTicket.model_validate(runtime.read('leader')['meta_demon_ticket'])
        owner = SimpleNamespace(config=SimpleNamespace(meta_demon_ticket=settings, cooperation_runtime=runtime),
                                device=SimpleNamespace(image=object()))
        runner = Mock()
        def run_leader():
            self.assertEqual(runner._ticket_peers, ('member1', 'member2'))
            for name in ('member1', 'member2'):
                member = TicketCooperation(self.runtimes[name], self.group, True)
                member.poll()
            runner._ticket_link.poll()
        runner.run_leader.side_effect = run_leader
        with patch('tasks.MetaDemonTicket.ticket_farming.tickets_full_before_farming', return_value=True), \
                patch('tasks.MetaDemonTicket.ticket_farming.AwakeTicketTask', return_value=runner):
            self.assertTrue(run_ticket_farming(owner, TicketMode.AWAKE))
        runner.run_leader.assert_called_once()
        for name in ('member1', 'member2'):
            state = self.runtimes[name].peer_battle_state('MetaDemonTicket', 'leader')
            self.assertTrue(state['payload']['complete'])
            self.assertTrue(state['payload']['finished'])


if __name__ == '__main__':
    unittest.main()
