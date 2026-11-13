from pydantic import Field

from tasks.Component.config_base import ConfigBase
from tasks.Component.config_scheduler import Scheduler


class BorrowShikigami(ConfigBase):
    """剧情、借用、探索和经验妖怪，最终确认账号达到15级。"""

    scheduler: Scheduler = Field(default_factory=Scheduler)
