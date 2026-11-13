from pydantic import BaseModel, Field

from tasks.Component.config_base import ConfigBase, dynamic_hide
from tasks.Component.config_scheduler import Scheduler


class LevelingProgress(BaseModel):
    account_name: str = ''
    last_level: int = 0
    completed_dungeons: list[str] = Field(default_factory=list)
    experience_attempted: bool = False


class BorrowShikigami(ConfigBase):
    """剧情、借用、觉醒御魂和经验妖怪，最终确认账号达到15级。"""

    scheduler: Scheduler = Field(default_factory=Scheduler)
    progress: LevelingProgress = Field(default_factory=LevelingProgress)

    hide_progress = dynamic_hide('progress')
