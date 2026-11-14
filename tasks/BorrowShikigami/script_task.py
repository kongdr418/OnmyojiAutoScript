from functools import partial
from types import SimpleNamespace

from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger
from tasks.AutoStory.script_task import ScriptTask as AutoStoryTask
from tasks.BorrowShikigami.assets import BorrowShikigamiAssets
from tasks.BorrowShikigami.child_tasks import (
    LevelingBattleFailed, LevelingBattleMixin, LevelingExperienceTask, handle_leveling_popup,
    register_beginner_battle,
)
from tasks.Component.config_base import Time
from tasks.Component.GeneralBattle.general_battle import BattleAction
from tasks.CustomTask.script_task import EntryConfig
import tasks.Exploration.page as exploration_pages
from tasks.Exploration.config import ExplorationLevel
from tasks.ExplorationFight.config import ExplorationFight, ExplorationFightConfig, ExplorationDifficulty
from tasks.ExplorationFight.script_task import ScriptTask as ExplorationFightTask
from tasks.ExperienceYoukai.config import ExperienceYoukai
from tasks.GameUi.page import all_of, any_of, page_main


def register_beginner_exploration(task):
    page = task.navigator.resolve_page(exploration_pages.page_exp_main)
    page.recognizer = any_of(page.recognizer, all_of(
        BorrowShikigamiAssets.I_BEGINNER_EXP_FIXED_TEAM,
        BorrowShikigamiAssets.I_BEGINNER_EXP_COST,
    ))


class FirstChapterTask(LevelingBattleMixin, ExplorationFightTask):
    def __init__(self, config, device, target_level: int | None = None):
        self.target_level = target_level
        self._result_level_candidate = None
        self._target_level_reached = False
        super().__init__(config, device)
        register_beginner_exploration(self)

    def _handle_result(self, context, config):
        if self.target_level is not None and not self._target_level_reached:
            text = BorrowShikigamiAssets.O_ACCOUNT_RESULT_LEVEL.ocr(self.device.image).strip()
            level = int(text) if text.isascii() and text.isdigit() and len(text) <= 2 else None
            if level is not None and self.target_level <= level <= 60:
                if level != self._result_level_candidate:
                    self._result_level_candidate = level
                    return BattleAction.CONTINUE
                self._target_level_reached = True
                logger.info(f'15级起号：结算页确认阴阳师等级 {level}，完成本阶段探索刷级')
            else:
                self._result_level_candidate = None
        return super()._handle_result(context, config)

    def run_general_battle(self, *args, **kwargs):
        self._result_level_candidate = None
        won = super().run_general_battle(*args, **kwargs)
        if self._target_level_reached:
            self.need_exit = True
            self.goto_page(exploration_pages.page_exploration, skip_first_screenshot=False)
            self.set_next_run('ExplorationFight', finish=True, success=True)
            raise TaskEnd
        return won


class ScriptTask(AutoStoryTask, BorrowShikigamiAssets):
    def __init__(self, config, device):
        super().__init__(config, device)
        register_beginner_battle(self)
        register_beginner_exploration(self)

    def screenshot(self):
        while True:
            result = super().screenshot()
            if not handle_leveling_popup(self):
                return result

    def _infer_task_category(self) -> str:
        return exploration_pages.page_exp_main.category

    def run(self) -> bool:
        success = self.run_leveling()
        self.set_next_run('BorrowShikigami', finish=True, success=success)
        if success:
            logger.info('15级起号完成：已确认阴阳师等级达到15级')
            raise TaskEnd
        logger.warning('15级起号尚未完成，等待重试')
        return False

    def run_leveling(self) -> bool:
        self.screenshot()
        level = self.read_account_level() if self._on_courtyard_main() else None
        if level is not None and level >= 15:
            return True
        if ((level is None or level < 7)
                and not self.appear(self.I_BEGINNER_EVENT_PAGE)
                and (self.appear(self.I_STORY_BACK)
                or self.appear(self.I_STORY_CHAT_1)
                or self.appear(self.I_STORY_CHAT_RIGHT_EDGE))):
            logger.info('15级起号：推进新号剧情')
            if not self.run_story():
                return False
        if not self.ensure_level_seven():
            return False
        logger.info('15级起号：进入新手特权借用待宵姑获鸟')
        if not self.borrow_ubume():
            return False
        return self.finish_leveling()

    def read_account_level(self):
        previous = None
        for _ in range(3):
            self.screenshot()
            if self.appear(self.I_ACCOUNT_LEVEL_SEVEN):
                level = 7
            else:
                text = self.O_ACCOUNT_LEVEL.ocr(self.device.image).strip()
                level = int(text) if text.isascii() and text.isdigit() and len(text) <= 2 else None
            if level is not None and 1 <= level <= 60:
                if level == previous:
                    logger.info(f'15级起号：确认阴阳师等级 {level}')
                    return level
                previous = level
            else:
                previous = None
        logger.warning('无法连续确认阴阳师等级，本轮暂停起号流程')
        return None

    def ensure_level_seven(self) -> bool:
        return self.ensure_account_level(7)

    def ensure_account_level(self, target_level: int) -> bool:
        self.screenshot()
        return_timeout = Timer(10).start()
        while self.appear(self.I_BEGINNER_EVENT_PAGE):
            if return_timeout.reached():
                logger.warning('无法从新手活动返回庭院确认等级，本轮暂停起号流程')
                return False
            self.click(self.C_BORROW_BACK, interval=1)
            self.screenshot()
        self.goto_page(page_main, skip_first_screenshot=False)
        level = self.read_account_level()
        if level is None:
            return False
        settings = ExplorationFight(exploration_fight_config=ExplorationFightConfig(
            difficulty=ExplorationDifficulty.SIMPLE,
            start_chapter=ExplorationLevel.EXPLORATION_1,
            end_chapter=ExplorationLevel.EXPLORATION_1,
        ))
        limit = settings.exploration_fight_config.limit_time
        timeout = Timer(limit.hour * 3600 + limit.minute * 60 + limit.second).start()
        while level < target_level and not timeout.reached():
            remaining = max(1, int(timeout.limit - timeout.current()))
            settings.exploration_fight_config.limit_time = Time(
                hour=remaining // 3600, minute=remaining // 60 % 60, second=remaining % 60,
            )
            logger.info(f'15级起号：当前 {level} 级，挑战普通第一章探索，目标 {target_level} 级')
            runner_type = partial(FirstChapterTask, target_level=target_level)
            if not self.run_child_task('ExplorationFight', settings, runner_type):
                logger.warning('第一章探索未完成，本轮暂停起号流程')
                return False
            self.goto_page(page_main, skip_first_screenshot=False)
            level = self.read_account_level()
            if level is None:
                return False
        if level < target_level:
            logger.warning(f'探索达到运行时间上限，尚未确认 {target_level} 级，本轮暂停起号流程')
            return False
        return True

    def run_child_task(self, task_type, settings, runner_type, required_wins=1) -> bool:
        config = EntryConfig(self.config, SimpleNamespace(type=task_type), settings)
        runner = runner_type(config, self.device)
        try:
            runner.run()
        except TaskEnd:
            pass
        except LevelingBattleFailed as error:
            logger.warning(f'15级起号：{error}')
            return False
        completed = config.success is True and runner.successful_battles >= required_wins
        if not completed:
            logger.warning(f'15级起号：{task_type} 本轮确认获胜 {runner.successful_battles}/{required_wins} 场')
        return completed

    def finish_leveling(self) -> bool:
        self.goto_page(page_main, skip_first_screenshot=False)
        level = self.read_account_level()
        if level is None:
            return False
        if level >= 15:
            return True
        if not self.ensure_account_level(10):
            return False
        logger.info('15级起号：执行现有经验妖怪任务')
        if not self.run_child_task('ExperienceYoukai', ExperienceYoukai(), LevelingExperienceTask, required_wins=2):
            logger.warning('经验妖怪未完成两场挑战，将通过普通探索补足等级')
        return self.ensure_account_level(15)

    def borrow_ubume(self) -> bool:
        self.screenshot()
        if not (self.appear(self.I_BORROW_PAGE)
                or self.appear(self.I_BEGINNER_EVENT_PAGE)
                or self.appear(self.I_BEGINNER_ENTRANCE)):
            self.goto_page(page_main, skip_first_screenshot=False)

        timeout = Timer(60).start()
        borrowed = False
        confirmed_frames = 0
        while not timeout.reached():
            self.screenshot()
            if self.appear(self.I_BORROW_PAGE):
                if borrowed:
                    self.click(self.C_BORROW_BACK, interval=1)
                    continue
                if self.appear(self.I_UBUME_BORROWED) or self.appear(self.I_UBUME_USING):
                    confirmed_frames += 1
                    if confirmed_frames >= 2:
                        logger.info('确认已借用待宵姑获鸟，返回庭院')
                        borrowed = True
                    continue
                confirmed_frames = 0
                self.appear_then_click(self.I_UBUME_CARD, interval=3)
                continue

            confirmed_frames = 0
            if self.appear(self.I_BEGINNER_EVENT_PAGE):
                if borrowed:
                    self.click(self.C_BORROW_BACK, interval=1)
                elif self.appear(self.I_BEGINNER_BORROW_ENTRY):
                    self.appear_then_click(self.I_BEGINNER_BORROW_ENTRY, interval=2)
                else:
                    self.appear_then_click(self.I_BEGINNER_PRIVILEGE, interval=2)
                continue
            if self.appear(self.I_BEGINNER_ENTRANCE) or self.appear(self.I_CHECK_MAIN):
                if borrowed:
                    return True
                self.appear_then_click(self.I_BEGINNER_ENTRANCE, interval=2)

        logger.warning('借用待宵姑获鸟超时，请检查新手特权入口、借用名额和当前页面')
        return False
