"""突破券 OCR 上限与无效读数处理回归检查。"""
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from module.exception import TaskEnd
from tasks.Exploration.base import BaseExploration
import tasks.Exploration.page as pages
from tasks.RealmRaid.script_task import ScriptTask


class RealmRaidTicketChecks(unittest.TestCase):
    def make_task(self, counters):
        task = object.__new__(ScriptTask)
        task.device = SimpleNamespace(image=object())
        task.O_NUMBER = SimpleNamespace(ocr=Mock(side_effect=counters))
        task.wait_until_appear = Mock()
        task.screenshot = Mock()
        task.reward_detect_click = Mock()
        task.config = SimpleNamespace(realm_raid=SimpleNamespace(
            raid_config=SimpleNamespace(number_attack=2)))
        task.init_tickets = -1
        return task

    def test_invalid_counts_are_discarded_until_a_valid_frame(self):
        task = self.make_task([(31, -1, 30), (999, -969, 30), (-1, 31, 30),
                               (30, 50, 80), (30, 0, 30)])
        self.assertEqual(task.read_ticket_count(), 30)
        self.assertEqual(task.screenshot.call_count, 5)
        task.reward_detect_click.assert_not_called()

    def test_failed_ocr_retries_and_preserves_valid_zero(self):
        task = self.make_task([(0, 0, 0), (0, 0, 0), (0, 30, 30)])
        self.assertEqual(task.read_ticket_count(), 0)
        self.assertEqual(task.reward_detect_click.call_count, 2)
        self.assertEqual(task.screenshot.call_count, 3)

    def test_invalid_counts_do_not_change_initial_or_consumed_tickets(self):
        task = self.make_task([(999, -969, 30), (20, 10, 30),
                               (88, -58, 30), (19, 11, 30), (18, 12, 30)])
        self.assertTrue(task.check_ticket(base=10))
        self.assertEqual(task.init_tickets, 20)
        self.assertTrue(task.check_ticket(base=10))
        self.assertEqual(task.init_tickets, 20)
        self.assertFalse(task.check_ticket(base=10))
        self.assertEqual(task.init_tickets, 20)

    def test_is_ticket_uses_a_valid_count_after_an_overflow(self):
        for current in (0, 30):
            with self.subTest(current=current):
                task = self.make_task([(31, -1, 30), (current, 30 - current, 30)])
                self.assertEqual(task.is_ticket(), current > 0)
                self.assertEqual(task.screenshot.call_count, 2)

    def test_valid_counts_still_enforce_the_starting_threshold(self):
        for current in (0, 4, 5, 30):
            with self.subTest(current=current):
                task = self.make_task([(current, 30 - current, 30)])
                self.assertEqual(task.check_ticket(base=5), current >= 5)

    def test_chat_overlay_requires_a_valid_ticket_counter(self):
        task = self.make_task([])
        del task.reward_detect_click
        task.ui_click_until_disappear = Mock()
        task.appear_then_click = Mock(return_value=False)
        task.O_TEXT = SimpleNamespace(ocr=Mock(side_effect=[
            '聊天消息', '31/30', '30/80', '30/30']))
        self.assertTrue(task.reward_detect_click(False))
        self.assertEqual(task.O_TEXT.ocr.call_count, 4)
        self.assertEqual(task.screenshot.call_count, 3)


class ExplorationTicketChecks(unittest.TestCase):
    def test_only_valid_ticket_counts_can_schedule_realm_raid(self):
        scrolls = SimpleNamespace(scrolls_enable=True, scrolls_threshold=25,
                                  scrolls_cd=timedelta(minutes=30))
        exploration = SimpleNamespace(buff_gold_50_click=False, buff_gold_100_click=False,
                                      buff_exp_50_click=False, buff_exp_100_click=False)
        for page in (pages.page_exploration, pages.page_exp_entrance):
            for current, total in ((31, 30), (999, 30), (30, 80), (0, 0),
                                   (-1, 30), (0, 30), (24, 30), (25, 30), (30, 30)):
                with self.subTest(page=page.name, current=current, total=total):
                    task = object.__new__(BaseExploration)
                    task.device = SimpleNamespace(image=object())
                    task.O_REALM_RAID_NUMBER_META = SimpleNamespace(ocr_digit_counter=Mock(
                        return_value=(current, total - current, total)))
                    task.O_REALM_RAID_NUMBER1_META = SimpleNamespace(ocr_digit_counter=Mock(
                        return_value=(current, total - current, total)))
                    task.goto_page = Mock()
                    task.set_next_run = Mock()
                    if total == 30 and 25 <= current <= 30:
                        with self.assertRaises(TaskEnd):
                            task.activate_realm_raid(scrolls, exploration, page)
                        self.assertEqual(task.set_next_run.call_count, 3)
                    else:
                        task.activate_realm_raid(scrolls, exploration, page)
                        task.goto_page.assert_not_called()
                        task.set_next_run.assert_not_called()
                    rule = task.O_REALM_RAID_NUMBER1_META if page == pages.page_exp_entrance \
                        else task.O_REALM_RAID_NUMBER_META
                    rule.ocr_digit_counter.assert_called_once_with(task.device.image)


if __name__ == '__main__':
    unittest.main()
