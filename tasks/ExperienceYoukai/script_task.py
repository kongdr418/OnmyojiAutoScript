# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey

from module.exception import TaskEnd
from module.logger import logger
from module.base.timer import Timer
from module.atom.click import RuleClick
import tasks.GameUi.page as pages

from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_team, page_shikigami_records
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralRoom.general_room import GeneralRoom
from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.ExperienceYoukai.assets import ExperienceYoukaiAssets


class ScriptTask(GameUi, GeneralBattle, GeneralRoom, GeneralInvite, SwitchSoul, ExperienceYoukaiAssets):

    def screenshot(self):
        while True:
            image = super().screenshot()
            if self.appear(self.I_FIRST_TEAM_DIALOG):
                self.appear_then_click(
                    self.I_FIRST_TEAM_DIALOG, action=self.C_FIRST_TEAM_DIALOG_DISMISS, interval=1,
                )
                continue
            if self.appear(self.I_FIRST_TEAM_PAGE) and self.appear(self.I_FIRST_TEAM_GUIDE):
                x, y, _, _ = self.I_FIRST_TEAM_GUIDE.roi_front
                tip = (x + 1, y + 1, 6, 6)
                action = RuleClick(roi_front=tip, roi_back=tip, name='first_team_guide_tip', profile='High')
                self.appear_then_click(self.I_FIRST_TEAM_GUIDE, action=action, interval=1)
                continue
            if self.appear(self.I_FIRST_TEAM_PAGE) and self.appear(self.I_FIRST_TEAM_CREATE_GUIDE):
                if self.appear_then_click(
                        self.I_FIRST_TEAM_CREATE_GUIDE, action=self.C_FIRST_TEAM_CREATE, interval=1):
                    self._first_team_create_pending = True
                continue
            return image

    def _first_team_creation_open(self) -> bool:
        return getattr(self, '_first_team_create_pending', False) and (
            self.appear(self.I_CREATE_ENSURE) or self.appear(self.I_CREATE_ENSURE_2)
        )

    def check_zones(self, name: str) -> bool:
        if name == '经验妖怪' and self._first_team_creation_open():
            return True
        return super().check_zones(name)

    def create_room(self, create_room_rule=None) -> bool:
        self.screenshot()
        if self._first_team_creation_open():
            self._first_team_create_pending = False
            return True
        return super().create_room(create_room_rule)

    def before_run(self):
        page_battle_result = self.navigator.resolve_page(pages.page_battle_result)
        page_battle_result.recognizer = pages.any_of(self.I_EXP_WIN, page_battle_result.recognizer)
        team_page = self.navigator.resolve_page(page_team)
        team_page.recognizer = pages.any_of(
            team_page.recognizer, pages.all_of(self.I_FIRST_TEAM_PAGE, self.I_CREATE_ROOM),
            self._first_team_creation_open,
        )

    def run(self):
        self.before_run()
        # 切换御魂
        if self.config.experience_youkai.switch_soul.enable:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul(self.config.experience_youkai.switch_soul.switch_group_team)

        if self.config.experience_youkai.switch_soul.enable_switch_by_name:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul_by_name(self.config.experience_youkai.switch_soul.group_name,
                                         self.config.experience_youkai.switch_soul.team_name)

        # 开启加成
        con = self.config.experience_youkai.experience_youkai
        if con.buff_exp_50_click or con.buff_exp_100_click:
            self.goto_page(page_main)
            self.open_buff()
            if con.buff_exp_50_click:
                self.exp_50()
            if con.buff_exp_100_click:
                self.exp_100()
            self.close_buff()
        count = 0
        while count < 2:
            self.goto_page(page_team)
            self.check_zones('经验妖怪')
            # 开始
            if not self.create_room():
                self.experience_exit(con)
            self.ensure_public()
            self.create_ensure()
            # 进入到了房间里面
            wait_timer = Timer(50)
            wait_timer.start()
            while 1:
                self.screenshot()

                if not self.is_in_room():
                    continue
                if wait_timer.reached():
                    # 超过时间依然挑战
                    logger.warning('Wait for too long and start the challenge')
                    self.click_fire()
                    count += 1
                    self.run_general_battle()
                    break
                if not self.appear(self.I_ADD_5_1):
                    # 有人进来了，可以进行挑战
                    logger.info('There is someone in the room and start the challenge')
                    self.click_fire()
                    count += 1
                    self.run_general_battle()
                    break
        # 退出 (要么是在组队界面要么是在庭院)
        self.experience_exit(con)

    def experience_exit(self, con):
        self.goto_page(page_main)
        if con.buff_exp_50_click or con.buff_exp_100_click:
            self.open_buff()
            if con.buff_exp_50_click:
                self.exp_50(False)
            if con.buff_exp_100_click:
                self.exp_100(False)
            self.close_buff()

        self.set_next_run(task='ExperienceYoukai', success=True, finish=False)
        raise TaskEnd('ExperienceYoukai')


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)
    t.screenshot()

    t.run()
