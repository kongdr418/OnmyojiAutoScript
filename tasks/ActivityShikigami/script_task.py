# This Python file uses the following encoding: utf-8
"""式神活动统一任务入口。"""

from module.logger import logger
from tasks.ActivityShikigami.activities.fake_god import FakeGodAct
from tasks.ActivityShikigami.activities.normal import NormalClimbAct
from tasks.ActivityShikigami.activities.rich_man import RichManAct
from tasks.ActivityShikigami.base_act import BaseAct
from tasks.ActivityShikigami.activities.exploration import ExplorationAct


ACTIVITY_METHOD_FIELDS = {
    '大富翁': 'run_rich_man',
    '伪神': 'run_fakegod',
    '爬塔': 'run_climb',
    '探索': 'run_exploration',
    '首领': 'run_boss',
}


class ScriptTask(ExplorationAct, RichManAct, NormalClimbAct, FakeGodAct, BaseAct):

    def run(self):
        self.before_run()
        sequence = self.conf.general_config.task_sequence_v
        logger.info(f'ActivityShikigami enabled sequence: {sequence}')
        for activity_name in sequence:
            if self.time_limit_reached():
                break
            method_name = ACTIVITY_METHOD_FIELDS[activity_name]
            getattr(self, method_name)()
        self.finish_activity_task()
