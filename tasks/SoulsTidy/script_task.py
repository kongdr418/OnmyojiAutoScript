# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import sleep
from datetime import timedelta, datetime, time
from cached_property import cached_property

from module.exception import TaskEnd
from module.logger import logger
from module.base.timer import Timer

from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_shikigami_records
from tasks.SoulsTidy.assets import SoulsTidyAssets
from tasks.SoulsTidy.config import SimpleTidy
from typing import Optional


class ScriptTask(GameUi, SoulsTidyAssets):
    def run(self):
        self.goto_page(page_shikigami_records)
        con = self.config.souls_tidy
        if con.simple_tidy.enable_greed or con.simple_tidy.enable_maneki:
            self.goto_souls()
            self.greed_maneki()
            self.goto_page(page_shikigami_records)

        self.set_next_run(task='SoulsTidy', success=True, finish=False)
        raise TaskEnd('SoulsTidy')

    def goto_souls(self):
        """
        进入到御魂的主界面
        :return:
        """
        while 1:
            self.screenshot()
            if self.appear(self.I_ST_GREED) and self.appear(self.I_ST_TIDY):
                break

            if self.appear_then_click(self.I_ST_REPLACE, interval=1):
                continue
            if self.appear_then_click(self.I_ST_SOULS, interval=1):
                continue
            if self.appear_then_click(self.I_ST_SOULS_CLOSE, interval=1):
                continue
            if self.click(self.C_ST_DETAIL, interval=2):
                continue
        # 御魂超过上限的提示
        self.ocr_appear_click(self.O_ST_OVERFLOW)
        logger.info('Enter souls page')

    def greed_maneki(self):
        """
        贪吃鬼和招财猫
        :return:
        """
        # 先是贪吃鬼
        if self.config.souls_tidy.simple_tidy.enable_greed:
            logger.hr('Greed Ghost')
            self.ui_click(self.I_ST_GREED, self.I_ST_GREED_HABIT)
            self.ui_click(self.I_ST_GREED_HABIT, self.I_ST_FEED_NOW)
            logger.info('Feed greed ghost')
            feed_count = 0
            while 1:
                self.screenshot()
                if self.appear(self.I_ST_UNSELECTED):
                    self.ui_click_until_disappear(self.I_ST_UNSELECTED)
                    continue
                if self.click_ui_confirm(interval=0.5):
                    continue
                if feed_count >= 3:
                    break
                if self.appear_then_click(self.I_ST_FEED_NOW, interval=3.5):
                    feed_count += 1
                    continue
            logger.info('Feed greed ghost done')
        # 关闭贪吃鬼, 进入奉纳
        while 1:
            self.screenshot()
            if self.appear(self.I_ST_CAT):
                # 出现招财猫
                break

            # https://github.com/runhey/OnmyojiAutoScript/issues/662
            if self.appear(self.I_ST_UNSELECTED):
                self.ui_click_until_disappear(self.I_ST_UNSELECTED)
                continue
            if self.click_ui_confirm(interval=0.5):
                continue

            if self.appear_then_click(self.I_ST_GREED_CLOSE, interval=0.7):
                continue
            if self.appear_then_click(self.I_ST_BONGNA, interval=1, threshold=0.6):
                continue
        if self.config.souls_tidy.simple_tidy.enable_maneki:
            logger.hr('Enter bongna')
            # 确保已弃置界面
            while True:
                self.screenshot()
                if self.appear(self.I_ST_ABANDONED_SELECTED):
                    break
                # 防止因为好友消息导致误点击到好友聊天界面
                if self.appear(self.I_UI_BACK_RED):
                    self.click(self.I_UI_BACK_RED, interval=0.8)
                    continue
                self.click(self.I_ST_ABANDONED_SELECTED, interval=1.5)
            self.pre_confirm()
            # 开始奉纳
            while True:
                found = self.find_discard_souls()
                if not found:
                    logger.info('No discardable soul found, exit')
                    break
                self.click(self.L_ONE, interval=2.5)
                self.screenshot()
                gold_amount = self.O_ST_GOLD.ocr(self.device.image)
                if not isinstance(gold_amount, int) or gold_amount == 0:
                    logger.warning('Gold amount not int or 0, skip')
                    continue
                # 点击奉纳收取奖励
                if not self.appear(self.I_ST_DONATE):
                    logger.warning('Donate button not appear, skip')
                    continue
                self.donate_and_collect_reward()
                logger.info('Donate one')

        logger.info('Bongna done')

    def pre_confirm(self):
        """前置确认：确保是按照等级来排序的"""
        logger.info('Sort by level')
        while 1:
            self.screenshot()
            # 防止因为好友消息导致误点击到好友聊天界面
            if self.appear(self.I_UI_BACK_RED):
                self.click(self.I_UI_BACK_RED, interval=0.8)
                continue
            if self.ocr_appear(self.O_ST_SORT_LEVEL_1):
                break
            if self.ocr_appear_click(self.O_ST_SORT_LEVEL_2, interval=0.6):
                continue
            if self.ocr_appear_click(self.O_ST_SORT_TIME, interval=2):
                continue
            if self.ocr_appear_click(self.O_ST_SORT_TYPE, interval=2):
                continue
            if self.ocr_appear_click(self.O_ST_SORT_LOCATION, interval=2):
                continue

    def find_discard_souls(self) -> bool:
        """寻找是否有可弃置的御魂
        :return: True有 False没有/识别失败
        """
        timeout_timer = Timer(3).start()
        interval_timer = Timer(0.6).start()
        while not timeout_timer.reached():
            self.screenshot()
            # 控制检测频率
            if not interval_timer.started():
                interval_timer.start()
            elif interval_timer.reached():
                interval_timer.reset()
            else:
                continue
            if self.appear(self.I_ST_SOUL_STACK) or self.appear(self.I_ST_SOUL_STACK_1):
                logger.info('Find stacked discard souls')
                return True
            if self.appear(self.I_ST_LEVEL_0):
                logger.info('Find level 0 discard souls')
                return True
            first_soul_level = self.O_ST_FIRST_LEVEL.ocr(self.device.image)
            if not first_soul_level or first_soul_level.strip() == '':
                continue
            if first_soul_level.strip() in ['+0', '古']:
                logger.info('Find level 0 discard souls')
                return True
        return False

    def donate_and_collect_reward(self):
        """点击奉纳和收取奖励"""
        while True:
            self.screenshot()
            if self.click_ui_confirm(interval=0.5):
                continue
            # 如果奉纳少就不是神赐而是获得奖励
            if self.ui_reward_appear_click():
                continue
            # 出现神赐, 就点击然后消失，
            if self.appear(self.I_ST_GOD_PRESENT):
                logger.info('God present appear')
                self.click(self.C_ST_GOD_PRSENT, interval=2)
                continue
            if self.appear(self.I_ST_LUCK):
                # 出现吉运
                logger.info('luck appear')
                self.click(self.C_ST_SOUL_OFFERING_REWARD, interval=2)
                continue
            if self.appear(self.I_ST_SMALL_LUCK):
                # 出现小吉运
                logger.info('small luck appear')
                self.click(self.C_ST_SOUL_OFFERING_REWARD, interval=2)
                continue
            if self.appear_then_click(self.I_ST_DONATE, interval=5.5):
                self.wait_until_appear(self.I_ST_GOLD, True, wait_time=5)
                continue
            if not self.appear(self.I_ST_GOLD):
                break

if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)

    #t.greed_maneki()
    t.run()
