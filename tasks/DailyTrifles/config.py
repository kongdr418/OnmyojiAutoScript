# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from datetime import datetime
from pydantic import BaseModel, Field, SerializationInfo, field_serializer

from tasks.Component.config_scheduler import Scheduler
from tasks.Component.config_base import ConfigBase, dynamic_hide, DateTime, MultiLine
from enum import Enum


class SummonType(str, Enum):
    default = '普通召唤'
    recall = '今忆召唤'


class DoneRecord(ConfigBase):
    courtyard_affairs_dt: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"))
    pickup_email_dt: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"))
    summon_dt: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"))
    guild_donate_dt: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"))
    luck_msg_dt: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"))
    store_sign_dt: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"))
    sushi_dt: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"))
    guild_donate_finish: bool = Field(default=False)


class IncomeSnapshot(ConfigBase):
    """一次收益快照。读不到的项目留 -1，不要用 0 顶替，否则差值会凭空多出来。"""
    dt: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"))
    # 庭院
    gold: int = Field(default=-1)
    jade: int = Field(default=-1)
    ap: int = Field(default=-1)
    # 秘卷屋
    sca_orochi_scales: int = Field(default=-1)
    sca_reverse_scale: int = Field(default=-1)
    sca_demon_souls: int = Field(default=-1)
    sca_picture_book: int = Field(default=-1)
    # 商城
    mystery_amulet: int = Field(default=-1)


class DailyGuildDonate(ConfigBase):
    enable: bool = Field(default=False)
    auto_get_rewards: bool = Field(default=True)
    notify_enable: bool = Field(default=False, description='guild_donate_notify_help')
    guild_member_list: MultiLine = Field(default='', description='guild_member_list_help')
    friend_list: MultiLine = Field(default='', description='invite_friend_list_help')
    name_check: bool = Field(default=True, description='guild_donate_name_check_help')

    @property
    def guild_member_list_v(self) -> list[str]:
        if not self.guild_member_list or self.guild_member_list == '' or self.guild_member_list.strip() == '' or \
                len(self.guild_member_list.split('\n')) == 0:
            return []
        return [line.strip() for line in self.guild_member_list.split('\n') if line.strip() != '']

    @property
    def friend_list_v(self) -> list[str]:
        if not self.friend_list or self.friend_list == '' or self.friend_list.strip() == '' or \
                len(self.friend_list.split('\n')) == 0:
            return []
        return [line.strip() for line in self.friend_list.split('\n') if line.strip() != '']


class DailyTriflesConfig(BaseModel):
    # 庭院事务
    courtyard_affairs: bool = Field(default=True)
    # 收取邮件
    pickup_email: bool = Field(default=True)
    one_summon: bool = Field(title='One Summon', default=False)
    # 召唤类型
    summon_type: SummonType = Field(default=SummonType.default, description='召唤类型')
    # 是否绘制神秘图案
    draw_mystery_pattern: bool = Field(title='Draw Mystery Pattern', default=False, description='是否绘制神秘图案')
    luck_msg: bool = Field(title='Luck Msg', default=False)
    store_sign: bool = Field(title='Store Sign', default=False, description='store_sign_help')
    # 每天购买体力数量
    buy_sushi_count: int = Field(title='Buy Sushi Count', default=-1)
    # 统计每日收益
    income_stat: bool = Field(title='Income Stat', default=False)

    hide_fields = dynamic_hide('draw_mystery_pattern')


class DailyTrifles(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    trifles_config: DailyTriflesConfig = Field(default_factory=DailyTriflesConfig)
    guild_donate: DailyGuildDonate = Field(default_factory=DailyGuildDonate)
    done_record: DoneRecord = Field(default_factory=DoneRecord)
    # 收益快照历史，每次运行追加一条
    income_history: list[IncomeSnapshot] = Field(default_factory=list)

    hide_fields = dynamic_hide('done_record')

    @field_serializer('income_history')
    def serialize_income_history(self, value: list[IncomeSnapshot], info: SerializationInfo):
        """快照列表要显式落成 dict，隐藏时用和 dynamic_hide 一样的哨兵值"""
        if info.context and info.context.get('hide', False):
            return 0xABCDEF
        return [item.model_dump() for item in value]

    def today_is_done(self, mode: str) -> bool:
        """对应mode今天是否已经完成"""
        done_dt = getattr(self.done_record, f'{mode}_dt', None)
        if done_dt is None:
            return False
        if mode == 'guild_donate':
            if done_dt.date() != datetime.today().date():
                self.done_record.guild_donate_finish = False
            return self.done_record.guild_donate_finish
        return done_dt.date() == datetime.today().date()
