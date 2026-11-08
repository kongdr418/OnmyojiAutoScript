"""超鬼王协同通道与双任务身份校验（含独立门票任务）。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from module.cooperation import Cooperation
from module.exception import ScriptError
from tasks.MetaDemon.assistance import AssistanceLink, GhostAssistant
from tasks.MetaDemon.config import AssistanceRole, MetaDemon, MetaDemonMode


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
        self.assertFalse(a.prepare(config, 'MetaDemon', 'assist', defer=False))
        config.task_delay.assert_not_called()

    def test_prepare_detects_ready_peer(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        b.publish('MetaDemon')
        config = SimpleNamespace(task_delay=Mock())
        self.assertTrue(a.prepare(config, 'MetaDemon', 'assist', defer=False))


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
