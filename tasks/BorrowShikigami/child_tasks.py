from module.base.timer import Timer
from module.logger import logger
from tasks.AutoStory.script_task import handle_startup_popup
from tasks.EvoZone.script_task import ScriptTask as EvoZoneTask
from tasks.ExperienceYoukai.script_task import ScriptTask as ExperienceYoukaiTask
from tasks.Orochi.script_task import ScriptTask as OrochiTask
from tasks.GameUi.page import any_of, page_team


class LevelingBattleFailed(Exception):
    pass


class LevelingBattleMixin:
    def __init__(self, config, device):
        super().__init__(config, device)
        self.successful_battles = 0

    def screenshot(self):
        while True:
            image = super().screenshot()
            if not handle_startup_popup(self):
                return image

    def run_general_battle(self, *args, **kwargs):
        won = super().run_general_battle(*args, **kwargs)
        if not won:
            raise LevelingBattleFailed('战斗未获胜，保留当前步骤以便重试')
        self.successful_battles += 1
        return won

    def check_layer(self, layer):
        if not super().check_layer(layer):
            raise LevelingBattleFailed(f'无法确认副本层数：{layer}')
        return True


class LevelingEvoZoneTask(LevelingBattleMixin, EvoZoneTask):
    def _infer_task_category(self) -> str:
        return 'evo_zone'


class LevelingOrochiTask(LevelingBattleMixin, OrochiTask):
    def _infer_task_category(self) -> str:
        return 'orochi'


class LevelingExperienceTask(LevelingBattleMixin, ExperienceYoukaiTask):
    def _infer_task_category(self) -> str:
        return 'experience_youkai'

    def before_run(self):
        super().before_run()
        team_page = self.navigator.resolve_page(page_team)
        team_page.recognizer = any_of(team_page.recognizer, self._team_entry_battle)

    def _team_entry_battle(self) -> bool:
        return getattr(self, '_entering_team', False) and self.is_in_battle(False)

    def goto_page(self, destination, *args, **kwargs):
        if destination != page_team:
            return super().goto_page(destination, *args, **kwargs)
        self._entering_team = True
        try:
            return super().goto_page(destination, *args, **kwargs)
        finally:
            self._entering_team = False

    def create_room(self, create_room_rule=None) -> bool:
        create_room_rule = self.I_CREATE_ROOM if create_room_rule is None else create_room_rule
        clicks = 0
        timeout = Timer(15).start()
        while not timeout.reached():
            self.screenshot()
            if (self.is_in_battle(False)
                    or self.appear(self.I_CREATE_ENSURE)
                    or self.appear(self.I_CREATE_ENSURE_2)):
                self._first_team_create_pending = False
                return True
            if clicks >= 4:
                break
            if self.appear_then_click(create_room_rule, interval=2):
                clicks += 1
        logger.warning('15级起号：经验妖怪建房后未确认建房弹窗或战斗页面')
        return False

    def enter_experience_battle(self) -> bool:
        self.screenshot()
        if self.is_in_battle(False):
            return True
        self.goto_page(page_team)
        if self.is_in_battle(False):
            return True
        if not self.check_zones('经验妖怪') or not self.create_room():
            return False
        if not self.is_in_battle(False):
            self.ensure_public()
            if not self.is_in_battle(False):
                self.create_ensure()
        wait_timer = Timer(50).start()
        while True:
            self.screenshot()
            if self.is_in_battle(False):
                return True
            if self.is_in_room(False):
                if wait_timer.reached() or not self.appear(self.I_ADD_5_1):
                    return self.click_fire() is not False
            elif wait_timer.reached():
                logger.warning('15级起号：等待经验妖怪队伍或战斗页面超时')
                return False

    def run(self):
        self.before_run()
        for _ in range(2):
            if not self.enter_experience_battle():
                break
            logger.info('15级起号：接管经验妖怪准备和战斗流程')
            self.run_general_battle()
        self.experience_exit(self.config.experience_youkai.experience_youkai)
