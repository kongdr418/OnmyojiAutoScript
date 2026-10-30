# This Python file uses the following encoding: utf-8
from enum import Enum

from pydantic import Field

from tasks.Component.config_base import ConfigBase, Time
from tasks.Component.config_scheduler import Scheduler
from tasks.Component.GeneralInvite.config_invite import InviteBaseConfig


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
    completion_count: int = Field(
        title='完成局数',
        default=1,
        ge=-1,
        le=20,
        description='队长决定，-1 表示无限制，队员忽略此项',
    )
    stop_when_full: bool = Field(
        title='刷满即停',
        default=True,
        description='刷满十把30万，队员忽略此项',
    )


class DraftDuel(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    draft_duel_config: DraftDuelConfig = Field(default_factory=DraftDuelConfig)
    invite_config: InviteBaseConfig = Field(default_factory=InviteBaseConfig)
