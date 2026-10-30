# This Python file uses the following encoding: utf-8
from enum import Enum

from pydantic import Field

from tasks.Component.config_base import ConfigBase, Time
from tasks.Component.config_scheduler import Scheduler
from tasks.Component.GeneralInvite.config_invite import InviteConfig


class UserStatus(str, Enum):
    LEADER = 'leader'
    MEMBER = 'member'


class DraftDuelConfig(ConfigBase):
    # 队长负责邀请、扣费；队员只等邀请
    user_status: UserStatus = Field(
        default=UserStatus.LEADER,
        description='draft_duel_user_status_help',
    )
    # 单次任务的时间上限，到点后不再开始新的一轮
    limit_time: Time = Field(
        default=Time(minute=30),
        description='limit_time_help',
    )


class DraftDuel(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    draft_duel_config: DraftDuelConfig = Field(default_factory=DraftDuelConfig)
    invite_config: InviteConfig = Field(default_factory=InviteConfig)
