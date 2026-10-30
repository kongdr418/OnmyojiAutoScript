# This Python file uses the following encoding: utf-8
from pydantic import Field

from tasks.Component.config_base import ConfigBase, Time
from tasks.Component.config_scheduler import Scheduler


class DraftDuelConfig(ConfigBase):
    # 单次任务的时间上限，到点后不再开始新的一轮
    limit_time: Time = Field(
        default=Time(minute=30),
        description='limit_time_help',
    )


class DraftDuel(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    draft_duel_config: DraftDuelConfig = Field(default_factory=DraftDuelConfig)
