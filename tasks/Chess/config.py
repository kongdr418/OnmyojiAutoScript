# This Python file uses the following encoding: utf-8

from pydantic import Field, field_validator, model_validator

from tasks.Component.config_base import ConfigBase, Time
from tasks.Component.config_scheduler import Scheduler
from tasks.Chess.strategy.lineup import LineupBond, oasx_lineup_choices, resolve_lineup_key


class ChessConfig(ConfigBase):
    """百鬼棋局循环与结束条件。"""

    @model_validator(mode='before')
    @classmethod
    def migrate_haiguo_to_custom(cls, value):
        if isinstance(value, dict):
            selected = getattr(value.get('lineup_bond'), 'value', value.get('lineup_bond'))
            if selected in ('海国', 'haiguo') or value.get('custom_lineup') == 'builtin:haiguo':
                value = dict(value)
                value['lineup_bond'] = '自定义阵容'
                value['custom_lineup'] = 'custom:haiguo'
        return value

    hyakki_draft_enabled: bool = Field(
        title='百鬼优先所需御魂', default=True,
    )

    lineup_bond: LineupBond = Field(
        title='选择阵容羁绊',
        default=LineupBond.QIJIAOSHAN,
    )

    custom_lineup: str = Field(title='自定义阵容标识', default='',
                               description='custom_lineup_help')

    @field_validator('lineup_bond', mode='before')
    @classmethod
    def migrate_removed_lineup(cls, value):
        raw = getattr(value, 'value', value)
        if raw in {'流火', '冥府', '大江山', '狐妖', '荒川',
                   'liuhuo', 'mingfu', 'dajiangshan', 'huyao', 'arakawa'}:
            return LineupBond.QIJIAOSHAN
        return value

    rank_protection: bool = Field(
        title='保段位',
        default=False,
    )

    run_count: int = Field(
        title='执行次数',
        default=1,
        ge=-1,
    )
    matchmaking_timeout_seconds: int = Field(
        title='匹配限制时间（秒）',
        default=60,
        ge=10,
    )
    limit_time: Time = Field(
        title='运行时间限制',
        default=Time(minute=30),
    )
    coin_full_exit: bool = Field(
        title='刷满鼬乐币',
        default=False,
    )
    continue_grigri_refresh_below_nine: bool = Field(
        title='刷新非前40%符咒',
        default=True,
    )


class Chess(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    chess_config: ChessConfig = Field(default_factory=ChessConfig)

    def oasx_args(self, groups):
        choices = oasx_lineup_choices()
        selected = self.chess_config.lineup_bond
        if getattr(selected, 'value', selected) == '自定义阵容':
            identity = self.chess_config.custom_lineup
            if not identity.startswith(('builtin:', 'custom:')):
                identity = 'custom:' + identity
        else:
            identity = 'builtin:' + resolve_lineup_key(selected)
        current = next((label for label, key in choices.items() if key == identity), None)
        labels = list(choices)
        if current is None:
            current = f'阵容不存在 [{identity}]'
            labels.append(current)
        items = groups.get('chess_config', [])
        for item in items:
            if item['name'] == 'lineup_bond':
                item.update(type='enum', enumEnum=labels, value=current,
                            default=next((label for label, key in choices.items()
                                          if key == 'builtin:qijiaoshan'), current))
        groups['chess_config'] = [item for item in items if item['name'] != 'custom_lineup']
        return groups

    def select_oasx_lineup(self, value):
        """Handle dynamic choices while retaining the old enum/id storage contract."""
        import re
        choices = oasx_lineup_choices()
        identity = choices.get(str(value))
        if identity is None and str(value).startswith(('builtin:', 'custom:')):
            identity = str(value)
        if identity is None:
            match = re.fullmatch(r'(内置|自定义) · .* \[([a-z][a-z0-9_]{0,47})\]', str(value))
            if match:
                identity = ('builtin:' if match[1] == '内置' else 'custom:') + match[2]
        if identity is None:
            return None
        if identity not in choices.values():
            return False
        self.chess_config = ChessConfig.model_validate({
            **self.chess_config.model_dump(), 'lineup_bond': '自定义阵容',
            'custom_lineup': identity,
        })
        return True
