from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from tasks.ActivityShikigami.config import ActivityShikigami
from tasks.Component.config_base import ConfigBase
from tasks.Component.config_scheduler import Scheduler
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.Exploration.config import Exploration
from tasks.MetaDemon.config import MetaDemon
from tasks.MetaDemonTicket.config import MetaDemonTicket
from tasks.RealmRaid.config import RealmRaid


class CustomTaskAccountInfo(AccountInfo):
    character: str = Field(
        default='',
        description='选填；留空时按目标服务器匹配，同服有多个角色时按默认登录流程进入。',
    )


class AccountSwitch(ConfigBase):
    account: CustomTaskAccountInfo = Field(default_factory=CustomTaskAccountInfo)


TASK_TYPES = {
    'Exploration': ('探索', Exploration),
    'RealmRaid': ('个人突破', RealmRaid),
    'MetaDemonTicket': ('鬼王门票', MetaDemonTicket),
    'MetaDemon': ('超鬼王', MetaDemon),
    'ActivityShikigami': ('式神活动', ActivityShikigami),
    'AccountSwitch': ('账号切换', AccountSwitch),
}
ENTRY_PREFIX = 'CustomTaskEntry-'


class CustomTaskEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, pattern=r'^[a-zA-Z0-9_-]+$')
    type: str
    settings: dict = Field(default_factory=dict)

    @field_validator('type')
    @classmethod
    def validate_type(cls, value):
        if value not in TASK_TYPES:
            raise ValueError('不支持此任务类型')
        return value

    def task_model(self):
        return TASK_TYPES[self.type][1].model_validate(self.settings)


class CustomTask(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    entries: list[CustomTaskEntry] = Field(default_factory=list)
    selected_entry_id: str = Field(default='')

    @field_validator('entries')
    @classmethod
    def validate_entries(cls, entries):
        if len({entry.id for entry in entries}) != len(entries):
            raise ValueError('自定义任务条目 ID 不能重复')
        for entry in entries:
            entry.settings = entry.task_model().model_dump(exclude={'scheduler'})
        return entries

    def entry(self, entry_id: str):
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    @property
    def selected_entry(self):
        return self.entry(self.selected_entry_id) or next(iter(self.entries), None)

    def entry_choices(self) -> dict[str, str]:
        return {
            f'{index}. {TASK_TYPES[entry.type][0]}': entry.id
            for index, entry in enumerate(self.entries, 1)
        }

    def oasx_args(self, result):
        def enum_row(name, title, value, options, description):
            return {
                'name': name,
                'title': title,
                'type': 'enum',
                'value': value,
                'default': value,
                'enumEnum': options,
                'description': description,
            }

        rows = [enum_row('add_task', '添加任务', '请选择要添加的任务',
                         ['请选择要添加的任务', *[label for label, _ in TASK_TYPES.values()]],
                         '选择后将任务添加到列表末尾。完成后重新进入本页，查看并设置该任务。')]
        entry = self.selected_entry
        if entry is not None:
            choices = self.entry_choices()
            selected = next(label for label, entry_id in choices.items() if entry_id == entry.id)
            rows.append(enum_row('selected_entry', '当前编辑任务', selected, list(choices),
                                 '选项按实际执行顺序排列。切换后重新进入本页，下面会显示所选任务的独立配置。'))
            rows.append(enum_row('entry_action', '任务操作', '请选择操作',
                                 ['请选择操作', '上移', '下移', '删除'],
                                 '对当前编辑任务执行操作。完成后重新进入本页查看结果。'))
        rows[0]['ui_group_title'] = {'zh_CN': '自定义任务列表', 'en_US': 'Task list'}
        result['task_list'] = rows
        return result

    def set_control(self, argument: str, value) -> bool:
        if argument == 'add_task':
            if value == '请选择要添加的任务':
                return True
            task_type = next((key for key, (label, _) in TASK_TYPES.items() if label == value), None)
            if task_type is None:
                return False
            self.add(task_type)
            return True
        if argument == 'selected_entry':
            entry_id = self.entry_choices().get(value)
            if entry_id is None:
                return False
            self.selected_entry_id = entry_id
            return True
        if argument != 'entry_action':
            return False
        if value == '请选择操作':
            return True
        entry = self.selected_entry
        if entry is None:
            return False
        if value == '删除':
            self.remove(entry.id)
            return True
        if value not in ('上移', '下移'):
            return False
        ids = [item.id for item in self.entries]
        index = ids.index(entry.id)
        target = index + (-1 if value == '上移' else 1)
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
            self.reorder(ids)
        return True

    def list_payload(self):
        return {
            'entries': [
                {'id': entry.id, 'type': entry.type, 'settings': entry.settings, 'title': TASK_TYPES[entry.type][0], 'editor': ENTRY_PREFIX + entry.id}
                for entry in self.entries
            ],
            'choices': [{'type': key, 'title': label} for key, (label, _) in TASK_TYPES.items()],
        }

    def add(self, task_type: str):
        if task_type not in TASK_TYPES:
            raise ValueError('不支持此任务类型')
        task = TASK_TYPES[task_type][1]()
        self.entries.append(CustomTaskEntry(type=task_type, settings=task.model_dump(exclude={'scheduler'})))
        self.selected_entry_id = self.entries[-1].id

    def reorder(self, ids: list[str]):
        entries = {entry.id: entry for entry in self.entries}
        if len(ids) != len(entries) or set(ids) != entries.keys():
            raise ValueError('任务列表已变化，请刷新后重试')
        selected = self.selected_entry
        self.selected_entry_id = selected.id if selected else ''
        self.entries = [entries[entry_id] for entry_id in ids]

    def remove(self, entry_id: str):
        entry = self.entry(entry_id)
        if entry is None:
            raise ValueError('此任务条目已删除')
        self.entries.remove(entry)
        if self.selected_entry_id == entry_id:
            self.selected_entry_id = self.entries[0].id if self.entries else ''
