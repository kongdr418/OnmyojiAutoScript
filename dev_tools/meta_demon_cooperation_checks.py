"""超鬼王协同通道与双任务身份校验（含独立门票任务）。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from module.cooperation import Cooperation
from module.exception import ScriptError, TaskEnd
from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.MetaDemonTicket.config import MetaDemonTicket
from tasks.MetaDemon.assistance import AssistanceLink, GhostAssistant
from tasks.MetaDemon.config import AssistanceRole, MetaDemon, MetaDemonMode, OfferingMode


def write_config(root, name, *, role, peer='', enable=True, mode=None, ticket=None):
    config = {
        'meta_demon': {
            'scheduler': {'enable': enable, 'next_run': '2026-01-01 00:00:00'},
            'meta_demon_config': {'mode': [m.value for m in (mode or [MetaDemonMode.BOSS_ASSIST])]},
            'assistance_config': {'role': role.value, 'cooperation_script': peer},
        }
    }
    if ticket:
        config['meta_demon_ticket'] = ticket
    folder = Path(root) / 'config'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{name}.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')


def ticket_config(user_status, *, peer='', enable=True, mode='门票刷取：觉醒'):
    return {
        'scheduler': {'enable': enable, 'next_run': '2026-01-01 00:00:00'},
        'general_config': {'mode': mode, 'user_status': user_status},
        'invite_config': {'cooperation_script': peer},
    }


class CooperationChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_config(self.root, 'offer', role=AssistanceRole.OFFERING, peer='assist')
        write_config(self.root, 'assist', role=AssistanceRole.ASSIST, peer='offer')

    def runtime(self, name='offer'):
        return Cooperation(name, root=self.root)

    def test_battle_state_round_trip_through_sqlite(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        a.publish_battle_state('MetaDemonAssistance', 'assist', 'session-1', {'portrait': 'png-base64'})
        state = b.peer_battle_state('MetaDemonAssistance', 'offer')
        self.assertEqual(state['session'], 'session-1')
        self.assertEqual(state['payload']['portrait'], 'png-base64')
        self.assertIsNotNone(state['updated'])
        a.publish_battle_state('MetaDemonAssistance', 'assist', 'session-2', {'expires': 1})
        self.assertEqual(b.peer_battle_state('MetaDemonAssistance', 'offer')['session'], 'session-2')
        self.assertIsNone(b.peer_battle_state('MetaDemonAssistance', 'missing'))

    def test_meta_demon_pair_requires_opposite_roles_and_assist_mode(self):
        a = self.runtime('offer')
        self.assertIsNotNone(a.validate('MetaDemon', 'assist'))
        write_config(self.root, 'assist', role=AssistanceRole.OFFERING, peer='offer')
        with self.assertRaises(ScriptError):
            a.validate('MetaDemon', 'assist')
        write_config(self.root, 'assist', role=AssistanceRole.ASSIST, peer='offer',
                     mode=[MetaDemonMode.BOSS_BATTLE])
        with self.assertRaises(ScriptError):
            a.validate('MetaDemon', 'assist')

    def test_independent_offer_cannot_be_used_as_a_cooperative_partner(self):
        path = self.root / 'config' / 'offer.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['meta_demon']['assistance_config']['offering_mode'] = OfferingMode.INDEPENDENT
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        with self.assertRaisesRegex(ScriptError, '不协同'):
            self.runtime('assist').validate('MetaDemon', 'offer')

    def test_running_assistant_detects_offer_switching_to_independent_mode(self):
        offer = self.runtime('offer')
        offer.publish('MetaDemon')
        task = SimpleNamespace(
            conf=MetaDemon(assistance_config={'role': '协助', 'cooperation_script': 'offer'}),
            config=SimpleNamespace(config_name='assist', cooperation_runtime=self.runtime('assist')),
        )
        link = AssistanceLink(task)
        self.assertTrue(link.peer_in_task())
        path = self.root / 'config' / 'offer.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['meta_demon']['assistance_config']['offering_mode'] = OfferingMode.INDEPENDENT
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        self.assertFalse(link.peer_in_task())

    def test_ticket_pair_requires_leader_member_and_same_mode(self):
        write_config(self.root, 'leader', role=AssistanceRole.OFFERING, ticket=ticket_config('leader', peer='member'))
        write_config(self.root, 'member', role=AssistanceRole.OFFERING, ticket=ticket_config('member', peer='leader'))
        leader = self.runtime('leader')
        self.assertTrue(leader.matches_meta_demon_tickets('member'))
        self.assertIsNotNone(leader.validate('MetaDemonTicket', 'member'))
        # 两个队长或玩法不同都不算合法配对。
        write_config(self.root, 'member', role=AssistanceRole.OFFERING, ticket=ticket_config('leader', peer='leader'))
        self.assertFalse(leader.matches_meta_demon_tickets('member'))
        with self.assertRaises(ScriptError):
            leader.validate('MetaDemonTicket', 'member')
        write_config(self.root, 'member', role=AssistanceRole.OFFERING,
                     ticket=ticket_config('member', peer='leader', mode='门票刷取：探索'))
        self.assertFalse(leader.matches_meta_demon_tickets('member'))

    def test_ticket_pair_requires_enabled_schedulers(self):
        write_config(self.root, 'leader', role=AssistanceRole.OFFERING, ticket=ticket_config('leader', peer='member'))
        write_config(self.root, 'member', role=AssistanceRole.OFFERING,
                     ticket=ticket_config('member', peer='leader', enable=False))
        self.assertFalse(self.runtime('leader').matches_meta_demon_tickets('member'))

    def write_ticket_group(self, *, leader_targets='member1\nmember2', member_targets='leader',
                           mode='门票刷取：觉醒'):
        for name, role, peer in (('leader', 'leader', leader_targets),
                                 ('member1', 'member', member_targets),
                                 ('member2', 'member', member_targets)):
            write_config(self.root, name, role=AssistanceRole.OFFERING,
                         ticket=ticket_config(role, peer=peer, mode=mode))

    def test_three_person_ticket_group_supports_one_sided_configuration(self):
        for leader_targets, member_targets in (('member1\nmember2', 'leader'),
                                               ('member1\nmember2', ''), ('', 'leader')):
            with self.subTest(leader_targets=leader_targets, member_targets=member_targets):
                self.write_ticket_group(leader_targets=leader_targets, member_targets=member_targets)
                leader = self.runtime('leader')
                self.assertEqual(leader.ticket_group(), ('leader', 'member1', 'member2'))
                self.assertEqual(leader.ticket_peers(), ('member1', 'member2'))
                for name in ('member1', 'member2'):
                    member = self.runtime(name)
                    self.assertEqual(member.ticket_peers(), ('leader',))
                    self.assertTrue(member.matches_meta_demon_tickets('leader'))
                    self.assertIsNotNone(leader.validate('MetaDemonTicket', name))
                    self.assertFalse(member.matches_meta_demon_tickets('member2' if name == 'member1' else 'member1'))

    def test_ticket_group_checks_all_members_and_room_capacity(self):
        self.write_ticket_group()
        leader = self.runtime('leader')
        for changes in ({'enable': False}, {'mode': '门票刷取：探索'}, {'peer': 'elsewhere'}):
            with self.subTest(changes=changes):
                settings = {'peer': 'leader', **changes}
                write_config(self.root, 'member2', role=AssistanceRole.OFFERING,
                             ticket=ticket_config('member', **settings))
                self.assertFalse(leader.matches_meta_demon_tickets('member1'))
        self.write_ticket_group(mode='门票刷取：探索')
        with self.assertRaisesRegex(ScriptError, '探索组队支持两人'):
            leader.ticket_group()
        self.write_ticket_group()
        write_config(self.root, 'member3', role=AssistanceRole.OFFERING,
                     ticket=ticket_config('member', peer='leader'))
        self.assertFalse(leader.matches_meta_demon_tickets('member1'))

    def test_ticket_group_prepares_both_peers_when_one_is_busy(self):
        self.write_ticket_group()
        leader, first, second = (self.runtime(name) for name in ('leader', 'member1', 'member2'))
        first.publish('ActivityShikigami')
        second.publish('')
        config = SimpleNamespace(task_delay=Mock())
        self.assertFalse(leader.prepare(config, 'MetaDemonTicket', 'member1', defer=False))
        self.assertEqual(first.requested(), [('MetaDemonTicket', 'leader')])
        self.assertEqual(second.requested(), [('MetaDemonTicket', 'leader')])
        config.task_delay.assert_not_called()
        first.publish('MetaDemonTicket')
        second.publish('MetaDemonTicket')
        self.assertTrue(leader.prepare(config, 'MetaDemonTicket', 'member1', defer=False))

    def test_ticket_group_wake_accepts_multi_line_leader_target(self):
        self.write_ticket_group(member_targets='')
        leader, member = self.runtime('leader'), self.runtime('member1')
        leader.publish('MetaDemonTicket')
        member.publish('')
        self.runtime('member2').publish('')
        self.assertTrue(leader.prepare(SimpleNamespace(), 'MetaDemonTicket', 'member1', defer=False))
        settings = MetaDemonTicket(scheduler={'enable': True}, general_config={'user_status': 'member'})
        config = SimpleNamespace(model=SimpleNamespace(meta_demon_ticket=settings),
                                 reload=Mock(), task_call=Mock())
        self.assertEqual(member.wake_next(config), 'MetaDemonTicket')
        config.task_call.assert_called_once_with('MetaDemonTicket', force_call=False)
        self.assertEqual(member.peers['MetaDemonTicket'], 'leader')

    def test_group_deferral_keeps_wake_requests_for_both_members(self):
        self.write_ticket_group()
        leader = self.runtime('leader')
        first, second = self.runtime('member1'), self.runtime('member2')
        first.publish('OtherTask')
        config = SimpleNamespace(task_delay=Mock(), model=SimpleNamespace(
            meta_demon_ticket=SimpleNamespace(scheduler=SimpleNamespace(next_run='later'))))
        with self.assertRaises(TaskEnd):
            leader.prepare(config, 'MetaDemonTicket', 'member1')
        self.assertEqual(first.requested(), [('MetaDemonTicket', 'leader')])
        self.assertEqual(second.requested(), [('MetaDemonTicket', 'leader')])
        config.task_delay.assert_called_once_with(task='MetaDemonTicket', success=False, server=False)
        self.assertEqual(config.task_runtime_outcome['peer'], 'member1')

    def test_filtered_wake_leaves_other_task_requests_pending(self):
        runtime = self.runtime('offer')
        with runtime.connect() as db:
            db.execute('INSERT INTO wakes VALUES (?, ?, ?, ?)', ('offer', 'MetaDemon', 'assist', 1))
        self.assertIsNone(runtime.wake_next(SimpleNamespace(), task_filter='MetaDemonTicket'))
        self.assertEqual(runtime.requested(), [('MetaDemon', 'assist')])

    def test_scheduler_checks_ticket_wakes_at_task_boundary_and_during_idle(self):
        from datetime import datetime, timedelta
        from script import Script

        runtime = Mock()
        runtime.wake_next.return_value = 'MetaDemonTicket'
        config = SimpleNamespace(start_watching=Mock())
        script = SimpleNamespace(config=config, _cooperation_runtime=runtime)
        self.assertFalse(Script.wait_until(script, datetime.now() + timedelta(hours=1)))
        runtime.wake_next.assert_called_once_with(config, task_filter='MetaDemonTicket')
        runtime.wake_next.reset_mock()
        task = SimpleNamespace(next_run=datetime.now() - timedelta(seconds=1), command='MetaDemonTicket')
        config.get_next = Mock(return_value=task)
        config.script = SimpleNamespace(anti_ban=None)
        script.anti_ban_guard = SimpleNamespace(wake_time=Mock(return_value=None))
        script._hoard_next_task = lambda candidate, now: candidate
        script.state_queue = None
        self.assertEqual(Script.get_next_task(script), 'MetaDemonTicket')
        runtime.wake_next.assert_called_once_with(config, task_filter='MetaDemonTicket')

    def test_two_room_checks_coexist_and_leader_waits_for_both(self):
        self.write_ticket_group()
        leader, first, second = (self.runtime(name) for name in ('leader', 'member1', 'member2'))
        for runtime in (leader, first, second):
            runtime.publish('MetaDemonTicket')
        task = SimpleNamespace(_room_cooperation=lambda: (leader, 'MetaDemonTicket', 'member1'),
                               _ticket_peers=('member1', 'member2'))
        self.assertFalse(GeneralInvite._cooperation_rooms_ready(task))
        tokens = task._cooperation_room_tokens.copy()
        for runtime in (first, second):
            self.assertEqual(runtime.pending_room_check('MetaDemonTicket', 'leader'), tokens[runtime.name])
        first.answer_room_check('MetaDemonTicket', 'leader', tokens['member1'], True)
        self.assertFalse(GeneralInvite._cooperation_rooms_ready(task))
        second.answer_room_check('MetaDemonTicket', 'leader', tokens['member2'], True)
        self.assertTrue(GeneralInvite._cooperation_rooms_ready(task))
        self.assertEqual(task._cooperation_room_tokens, {})

    def test_old_room_check_schema_migrates_with_pending_request(self):
        folder = self.root / 'data' / 'cooperation'
        folder.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(folder / 'state.sqlite3') as db:
            db.execute('CREATE TABLE room_checks (source TEXT PRIMARY KEY, target TEXT, task TEXT, '
                       'token TEXT, created REAL, ready INTEGER, answered REAL)')
            db.execute('INSERT INTO room_checks VALUES (?, ?, ?, ?, ?, NULL, NULL)',
                       ('leader', 'member1', 'MetaDemonTicket', 'old-token', 1))
        runtime = self.runtime('leader')
        runtime.request_room_check('MetaDemonTicket', 'member2')
        with runtime.connect() as db:
            rows = db.execute('SELECT target, token FROM room_checks ORDER BY target').fetchall()
        self.assertEqual(rows[0], ('member1', 'old-token'))
        self.assertEqual(rows[1][0], 'member2')

    def test_legacy_meta_demon_reads_are_split_on_demand(self):
        legacy = {
            'meta_demon': {
                'scheduler': {'enable': True, 'next_run': '2026-01-01 00:00:00'},
                'meta_demon_config': {'mode': ['门票刷取：探索']},
            }
        }
        folder = self.root / 'config'
        (folder / 'legacy.json').write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
        data = self.runtime('legacy').read('legacy')
        self.assertEqual(data['meta_demon']['meta_demon_config']['mode'], ['鬼王战斗'])
        self.assertEqual(data['meta_demon_ticket']['general_config']['mode'], '门票刷取：探索')

    def test_prepare_reports_skip_when_peer_is_elsewhere(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        b.publish('ActivityShikigami')
        config = SimpleNamespace(task_delay=Mock())
        with patch('module.cooperation.time.sleep') as sleep:
            self.assertFalse(a.prepare(config, 'MetaDemon', 'assist', defer=False))
        sleep.assert_not_called()
        config.task_delay.assert_not_called()

    def test_prepare_detects_ready_peer(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        b.publish('MetaDemon')
        config = SimpleNamespace(task_delay=Mock())
        with patch('module.cooperation.time.sleep') as sleep:
            self.assertTrue(a.prepare(config, 'MetaDemon', 'assist', defer=False))
        sleep.assert_not_called()

    def test_prepare_rechecks_missing_and_expired_heartbeats_for_thirty_seconds(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        config = SimpleNamespace(task_delay=Mock())
        for expired in (False, True):
            with self.subTest(expired=expired):
                if expired:
                    b.publish('MetaDemon')
                    with b.connect() as db:
                        db.execute('UPDATE runners SET heartbeat=heartbeat-60 WHERE name=?', ('assist',))
                with patch('module.cooperation.time.monotonic', side_effect=(0, 0, 5, 10, 15, 20, 25, 30)), \
                        patch('module.cooperation.time.sleep') as sleep:
                    self.assertFalse(a.prepare(config, 'MetaDemon', 'assist', defer=False))
                self.assertEqual(sleep.call_args_list, [call(5)] * 6)
                self.assertEqual(b.requested(), [])
        config.task_delay.assert_not_called()

    def test_prepare_continues_when_peer_heartbeat_arrives_during_retry(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        config = SimpleNamespace(task_delay=Mock())
        for peer_task in ('', 'MetaDemon'):
            with self.subTest(peer_task=peer_task):
                with b.connect() as db:
                    db.execute('DELETE FROM runners WHERE name=?', ('assist',))
                with patch('module.cooperation.time.monotonic', return_value=0), \
                        patch('module.cooperation.time.sleep', side_effect=lambda seconds: b.publish(peer_task)) as sleep:
                    self.assertTrue(a.prepare(config, 'MetaDemon', 'assist', defer=False))
                sleep.assert_called_once_with(5)
                if peer_task == '':
                    self.assertEqual(b.requested(), [('MetaDemon', 'offer')])
                else:
                    self.assertEqual(b.requested(), [])

    def test_prepare_accepts_peer_heartbeat_on_last_retry(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        config = SimpleNamespace(task_delay=Mock())
        elapsed = 0

        def advance_time(seconds):
            nonlocal elapsed
            elapsed += seconds
            if elapsed == 30:
                b.publish('MetaDemon')

        with patch('module.cooperation.time.monotonic', side_effect=lambda: elapsed), \
                patch('module.cooperation.time.sleep', side_effect=advance_time) as sleep:
            self.assertTrue(a.prepare(config, 'MetaDemon', 'assist', defer=False))
        self.assertEqual(sleep.call_args_list, [call(5)] * 6)


class AssistanceLinkChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_task(self, *, role, peer, self_name='offer'):
        from datetime import datetime, timedelta
        conf = MetaDemon(assistance_config={'role': role.value, 'cooperation_script': peer,
                                           'friend_names': 'Alice'})
        config = SimpleNamespace(config_name=self_name, cooperation_runtime=Cooperation(self_name, root=self.root))
        return SimpleNamespace(conf=conf, config=config, start_time=datetime.now(),
                               limit_time=timedelta(minutes=30))

    def test_peer_with_same_role_is_rejected(self):
        write_config(self.root, 'assist', role=AssistanceRole.OFFERING, peer='offer')
        task = self.make_task(role=AssistanceRole.OFFERING, peer='assist')
        with self.assertRaises(ScriptError):
            AssistanceLink(task)

    def test_pair_accepts_opposite_roles(self):
        write_config(self.root, 'assist', role=AssistanceRole.ASSIST, peer='offer')
        task = self.make_task(role=AssistanceRole.OFFERING, peer='assist')
        link = AssistanceLink(task)
        self.assertEqual(link.peer, 'assist')
        link.publish('portrait-b64')
        peer = Cooperation('assist', root=self.root).peer_battle_state(link.CHANNEL, 'offer')
        self.assertIsNotNone(peer)
        self.assertEqual(peer['payload']['portrait'], 'portrait-b64')
        self.assertFalse(link.confirmed())


class GhostAssistantChecks(unittest.TestCase):
    def make_task(self):
        from tasks.MetaDemon.assets import MetaDemonAssets
        return SimpleNamespace(goto_page=Mock(), screenshot=Mock(), appear=Mock(),
                               device=SimpleNamespace(image=None, image_frame_id='frame'),
                               S_MD_SWIPE_UP=MetaDemonAssets.S_MD_SWIPE_UP)

    def test_invalid_portrait_does_not_touch_the_screen(self):
        task = self.make_task()
        GhostAssistant(task, Mock()).scan(('session', 0.0, 'not-base64!!'))
        task.screenshot.assert_not_called()

    def test_empty_portrait_is_rejected(self):
        task = self.make_task()
        GhostAssistant(task, Mock()).scan(('session', 0.0, ''))
        task.screenshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
