"""超鬼王协助：只分享六星、等待结算、双端身份与协同必经。"""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from pydantic import ValidationError

from module.config.config_model import ConfigModel
from module.exception import RequestHumanTakeover, ScriptError, TaskEnd
from tasks.MetaDemon.assistance import AssistanceLink, GhostOffering, run_assistance
from tasks.MetaDemon.config import AssistanceRole, BossType, MetaDemon
from tasks.MetaDemon.script_task import ScriptTask


class StubLink:
    """协助通道的测试替身：可控制对端是否在线、是否已确认。"""

    def __init__(self, peer=True, confirmed=False, peer_settled=False):
        self.peer = 'peer' if peer else ''
        self._confirmed = Mock(return_value=confirmed)
        self._settled = Mock(return_value=peer_settled)
        self.publish = Mock()
        self.confirm = Mock()

    def peer_in_task(self):
        return bool(self.peer)

    def confirmed(self):
        return self._confirmed()

    def peer_settled(self):
        return self._settled()

    def target(self):
        return None


class AssistanceChecks(unittest.TestCase):
    def make_task(self, **options):
        conf = MetaDemon(assistance_config={'friend_names': 'Alice;Bob', **options})
        t = SimpleNamespace(conf=conf, config=SimpleNamespace(meta_demon=conf),
            start_time=datetime.now(), limit_time=timedelta(minutes=5), limit_count=10,
            total_count=0, State=SimpleNamespace(done=False), cur_boss_type=BossType.SIX_STARS,
            _boss_generation=1, device=SimpleNamespace(image=np.zeros((720, 1280, 3), dtype=np.uint8),
                                                       image_frame_id='frame'),
            screenshot=Mock(), swipe=Mock(), click=Mock(), appear_then_click=Mock(return_value=True),
            check_and_switch_ticket=Mock(return_value=True), start_battle=Mock(),
            set_next_run=Mock(), before_run=Mock(), goto_page=Mock(), finish_task=Mock())
        for name in ('I_MD_FIRE', 'I_MD_SUMMON_BOSS', 'I_MD_SETTLING', 'I_MD_INVITE',
                     'I_MD_INVITE_FRIENDS', 'I_UI_REWARD', 'C_UI_REWARD'):
            setattr(t, name, object())
        t.I_SHARE_DEMON = SimpleNamespace(roi_back=(0, 0, 80, 82))
        t.phase = 'fire'
        t.appear = Mock(side_effect=lambda rule: rule is {
            'fire': t.I_MD_FIRE, 'empty': t.I_MD_SUMMON_BOSS,
            'settling': t.I_MD_SETTLING, 'reward': t.I_UI_REWARD,
            'panel': t.I_MD_INVITE_FRIENDS}.get(t.phase))
        t.O_MD_FRIENDS = SimpleNamespace(roi=(435, 179, 486, 351), detect_and_ocr=Mock())
        t.I_MD_FRIEND_SELECTED = SimpleNamespace(match_all=Mock(return_value=[]))
        t.S_MD_SWIPE_FRIEND_UP = SimpleNamespace(roi_front=(784,398,55,20), roi_back=(792,340,56,23))
        return t

    def test_modes_are_boss_battle_or_assist_only(self):
        conf = MetaDemon(meta_demon_config={'mode': ['鬼王协助']}, assistance_config={'friend_names': ' Alice ;Bob\nAlice；Carol '})
        self.assertEqual([mode.value for mode in conf.meta_demon_config.mode], ['鬼王协助'])
        self.assertEqual(conf.assistance_config.friends, ['Alice', 'Bob', 'Carol'])
        self.assertEqual(conf.assistance_config.role, AssistanceRole.OFFERING)
        with self.assertRaises(ValidationError):
            MetaDemon(meta_demon_config={'mode': ['鬼王战斗', '鬼王协助']})
        # 门票刷取已迁出本任务，旧值不应再被接受。
        with self.assertRaises(ValidationError):
            MetaDemon(meta_demon_config={'mode': ['门票刷取：探索']})
        payload = ConfigModel.script_task(SimpleNamespace(meta_demon=conf), 'MetaDemon')
        self.assertTrue(all('default' in row for row in payload['assistance_config']))

    def test_shares_once_waits_for_settlement_then_summons_only_six(self):
        t = self.make_task()
        offering = GhostOffering(t, StubLink())
        offering.share = Mock(return_value=True)
        self.assertTrue(offering.tick())
        self.assertTrue(offering.tick())
        offering.share.assert_called_once()
        # 结算画面未出现前不计数、也不召唤下一只。
        t.phase = 'empty'
        offering.tick()
        self.assertEqual(t.total_count, 0)
        t.check_and_switch_ticket.assert_not_called()
        # 结算后鬼王消失才算这一轮结束。
        t.phase = 'settling'
        offering.tick()
        t.phase = 'empty'
        offering.tick()
        offering.tick()
        self.assertEqual(t.total_count, 1)
        t.check_and_switch_ticket.assert_called_once_with(sequence=(BossType.SIX_STARS,))
        t.start_battle.assert_not_called()

    def test_unknown_or_low_existing_boss_is_never_shared(self):
        for star in (None, BossType.ONE_STAR, BossType.FIVE_STARS):
            t = self.make_task()
            t.cur_boss_type = star
            offering = GhostOffering(t, StubLink())
            offering.share = Mock(return_value=True)
            offering.tick()
            offering.tick()
            offering.share.assert_not_called()
            t.check_and_switch_ticket.assert_not_called()
            t.start_battle.assert_not_called()

    def test_share_refuses_low_stars(self):
        t = self.make_task()
        offering = GhostOffering(t, StubLink())
        offering.open_share_panel = Mock(return_value=True)
        offering.select_friends = Mock(return_value=True)
        for star in range(1, 6):
            t.cur_boss_type = BossType(star)
            self.assertFalse(offering.share())
        offering.open_share_panel.assert_not_called()

    def test_share_requires_peer_confirmation_after_sending_portrait(self):
        t = self.make_task()
        link = StubLink()
        link._confirmed = Mock(side_effect=[False, False, True])
        offering = GhostOffering(t, link)
        offering.open_share_panel = Mock(return_value=True)
        offering.select_friends = Mock(return_value=True)
        offering.send_portrait = Mock(return_value=True)
        t.phase = 'fire'
        self.assertTrue(offering.share())
        offering.send_portrait.assert_called_once()

    def test_share_stops_when_peer_left_the_task(self):
        t = self.make_task()
        offering = GhostOffering(t, StubLink(peer=False))
        offering.open_share_panel = Mock(return_value=True)
        offering.select_friends = Mock(return_value=True)
        self.assertFalse(offering.share())
        offering.open_share_panel.assert_not_called()

    def test_named_matching_is_exact_and_boxes_use_resource_roi(self):
        t = self.make_task()
        t.O_MD_FRIENDS.detect_and_ocr.return_value = [SimpleNamespace(
            ocr_text=' Alice ', box=np.array([[5,10],[45,10],[45,30],[5,30]]))]
        rows = GhostOffering(t, StubLink()).friend_rows()
        self.assertEqual(rows, {'Alice': [(440, 189, 40, 20)]})
        self.assertNotIn('Alic', rows)

    def test_missing_similar_name_does_not_send_partial_selection(self):
        t = self.make_task(friend_names='Alice')
        t.phase = 'panel'
        offering = GhostOffering(t, StubLink())
        offering.list_to_top = Mock(return_value=True)
        offering.friend_rows = Mock(return_value={'Alicex': [(1,2,3,4)]})
        offering.select_friend = Mock(return_value=True)
        self.assertFalse(offering.select_friends())
        offering.select_friend.assert_not_called()
        t.appear_then_click.assert_not_called()

    def test_visible_friends_are_selected_without_scrolling(self):
        t = self.make_task()
        t.phase = 'panel'
        offering = GhostOffering(t, StubLink())
        offering.list_to_top = Mock(return_value=True)
        offering.friend_rows = Mock(return_value={'Alice': [(1,2,3,4)], 'Bob': [(5,6,7,8)]})
        offering.select_friend = Mock(return_value=True)
        self.assertTrue(offering.select_friends())
        self.assertEqual(offering.select_friend.call_count, 2)
        offering.list_to_top.assert_not_called()
        t.appear_then_click.assert_not_called()

    def test_old_selections_are_not_sent_to_unconfigured_people(self):
        t = self.make_task()
        t.phase = 'panel'
        offering = GhostOffering(t, StubLink())
        offering.list_to_top = Mock(return_value=True)
        offering.selected_count = Mock(return_value=1)
        self.assertFalse(offering.select_friends())
        t.appear_then_click.assert_not_called()

    def test_friend_click_waits_for_selected_marker_count(self):
        t = self.make_task()
        t.phase = 'panel'
        t.I_MD_FRIEND_SELECTED.match_all.side_effect = [[], [], [object()]]
        offering = GhostOffering(t, StubLink())
        self.assertTrue(offering.select_friend('Alice', (440, 189, 40, 20)))
        self.assertEqual(t.click.call_args.args[0].roi_front, (440, 189, 40, 20))
        self.assertEqual(t.screenshot.call_count, 2)

    def test_share_failure_never_records_success_or_enters_battle(self):
        t = self.make_task()
        offering = GhostOffering(t, StubLink())
        offering.share = Mock(return_value=False)
        self.assertFalse(offering.tick())
        self.assertIsNone(offering.shared_generation)
        self.assertEqual(t.total_count, 0)
        t.start_battle.assert_not_called()

    def test_kill_limit_prevents_another_summon(self):
        t = self.make_task()
        t.limit_count = 1
        offering = GhostOffering(t, StubLink())
        offering.share = Mock(return_value=True)
        t.phase = 'fire'
        offering.tick()
        t.phase = 'settling'
        offering.tick()
        t.phase = 'empty'
        offering.tick()
        offering.tick()
        self.assertFalse(offering.within_limit())
        t.check_and_switch_ticket.assert_not_called()

    def test_assist_without_peer_requests_human_takeover(self):
        t = self.make_task(role=AssistanceRole.ASSIST)
        with patch('tasks.MetaDemon.assistance.AssistanceLink') as link_class:
            link_class.return_value = SimpleNamespace(peer='')
            with self.assertRaises(RequestHumanTakeover):
                run_assistance(t)
        t.before_run.assert_called_once()

    def test_offering_without_peer_skips_this_run(self):
        t = self.make_task(friend_names='Alice')
        with patch('tasks.MetaDemon.assistance.AssistanceLink') as link_class:
            link_class.side_effect = ScriptError('未匹配')
            with self.assertRaises(TaskEnd):
                run_assistance(t)
        t.set_next_run.assert_called_once_with(task='MetaDemon', success=False)

    def test_offering_without_friends_requests_human_takeover(self):
        t = self.make_task(friend_names='')
        with self.assertRaises(RequestHumanTakeover):
            run_assistance(t)
        t.before_run.assert_not_called()

    def test_mode_dispatches_to_assistance(self):
        t = object.__new__(ScriptTask)
        t.config = SimpleNamespace(meta_demon=MetaDemon(meta_demon_config={'mode': ['鬼王协助']}))
        with patch('tasks.MetaDemon.assistance.run_assistance', return_value='offering') as run:
            self.assertEqual(t.run(), 'offering')
            run.assert_called_once_with(t)


class AssistanceLinkChecks(unittest.TestCase):
    def test_peer_must_have_opposite_role(self):
        task = SimpleNamespace(
            conf=MetaDemon(assistance_config={'role': '上供', 'cooperation_script': 'peer'}),
            config=SimpleNamespace(config_name='self'),
        )
        runtime = Mock()
        runtime.name = 'self'
        runtime.read.return_value = {'meta_demon': {
            'meta_demon_config': {'mode': ['鬼王协助']},
            'assistance_config': {'role': '上供', 'cooperation_script': 'self'},
        }}
        task.config.cooperation_runtime = runtime
        with self.assertRaises(ScriptError):
            AssistanceLink(task)


if __name__ == '__main__':
    unittest.main()
