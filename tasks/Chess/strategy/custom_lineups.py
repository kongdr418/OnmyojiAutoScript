"""Validated custom lineups shared by the local editor and Chess runtime."""
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, Field, model_validator

from tasks.Chess.strategy.shikigami_catalog import SHIKIGAMI_BY_ROMAJI
from tasks.Chess.strategy.soul_catalog import SOUL_BY_ROMAJI

ROOT = Path(__file__).resolve().parents[3]
STORE = Path(__file__).with_suffix('.json')
EMBLEMS = json.loads((ROOT / 'tasks/Chess/resource/fetter/portraits/index.json').read_text(encoding='utf-8'))['entries']
BONDS = sorted({bond for entry in SHIKIGAMI_BY_ROMAJI.values() for bond in entry.bonds})


class Equipment(BaseModel):
    kind: Literal['soul', 'emblem']
    key: str


class Slot(BaseModel):
    shikigami: str | None = None
    weight: int = Field(default=1, ge=0, le=99)
    equipment: list[Equipment] = Field(default_factory=list, max_length=2)


def lineup_bond_counts(slots):
    counts = Counter()
    for slot in slots:
        if slot.shikigami:
            counts.update(SHIKIGAMI_BY_ROMAJI[slot.shikigami].bonds)
        for item in slot.equipment:
            if item.kind == 'emblem':
                counts[EMBLEMS[item.key]['name'].removesuffix('纹章')] += 1
    return counts


def goldfish_position(slots, preferred=None):
    """Keep a free assigned position; otherwise fill backwards, or wait if full."""
    if preferred is not None and not slots[preferred - 1].shikigami:
        return preferred
    return next((index for index in range(12, 0, -1)
                 if not slots[index - 1].shikigami), None)


class CustomLineup(BaseModel):
    key: str = Field(pattern=r'^[a-z][a-z0-9_]{0,47}$')
    display_name: str = Field(min_length=1, max_length=40)
    slots: list[Slot] = Field(min_length=12, max_length=12)
    primary_bond: str = ''
    secondary_bond: str = ''
    hakuzosu_protect_position: int | None = Field(default=None, ge=1, le=12)
    arakawa_goldfish_position: int | None = Field(default=None, ge=1, le=12)

    @model_validator(mode='after')
    def valid_assignment(self):
        self.display_name = self.display_name.strip()
        if not self.display_name:
            raise ValueError('阵容名称不能为空')
        names = []
        for slot in self.slots:
            if slot.shikigami is None:
                if slot.equipment:
                    raise ValueError('空槽位不能装备御魂或纹章')
                continue
            if slot.shikigami not in SHIKIGAMI_BY_ROMAJI:
                raise ValueError(f'未知式神：{slot.shikigami}')
            names.append(slot.shikigami)
            seen = set()
            for equipment in slot.equipment:
                library = SOUL_BY_ROMAJI if equipment.kind == 'soul' else EMBLEMS
                identity = (equipment.kind, equipment.key)
                if equipment.key not in library:
                    raise ValueError(f'未知装备：{equipment.key}')
                if identity in seen:
                    raise ValueError('同一式神不能重复装备相同御魂或纹章')
                if equipment.kind == 'emblem' and EMBLEMS[equipment.key]['name'].removesuffix('纹章') in SHIKIGAMI_BY_ROMAJI[slot.shikigami].bonds:
                    raise ValueError('式神已具有该羁绊，不能装备对应纹章')
                seen.add(identity)
        if not 1 <= len(names) <= 10:
            raise ValueError('阵容需要 1–10 名式神')
        if len(names) != len(set(names)):
            raise ValueError('阵容中不能重复放置同名式神')
        for bond in (self.primary_bond, self.secondary_bond):
            if bond and bond not in BONDS:
                raise ValueError(f'未知羁绊：{bond}')
        counts = lineup_bond_counts(self.slots)
        if self.secondary_bond and not counts.get(self.secondary_bond):
            raise ValueError('次要羁绊必须是当前阵容已有的羁绊')
        if counts.get('荒川', 0) >= 2:
            self.arakawa_goldfish_position = goldfish_position(
                self.slots, self.arakawa_goldfish_position)
        else:
            self.arakawa_goldfish_position = None
        if self.hakuzosu_protect_position:
            if not self.slots[self.hakuzosu_protect_position - 1].shikigami:
                raise ValueError('守护之印目标必须是已上阵式神')
            if 'yume_san_byakuzou' not in names:
                raise ValueError('使用守护之印需要上阵梦山白藏主')
        return self

    def strategy(self, prefix='custom:'):
        from tasks.Chess.strategy.lineup_loader import strategy_from_document
        return strategy_from_document(self, prefix)


def _legacy_document(config):
    from tasks.Chess.strategy.shikigami_catalog import build_lineup_shikigami
    slots = [Slot() for _ in range(12)]
    for name, unit in build_lineup_shikigami(config.get('shikigami_positions', {})).items():
        slots[unit['position'] - 1] = Slot(
            shikigami=name, weight=unit['deploy_weight'],
            equipment=[Equipment(kind='soul', key=key) for key in unit['preferred_souls']])
    return {**{key: value for key, value in config.items() if key != 'shikigami_positions'},
            'slots': [slot.model_dump() for slot in slots]}


def read_collection(path):
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding='utf-8'))
    if 'version' in raw:
        if raw['version'] not in (1, 2) or not isinstance(raw.get('lineups'), list):
            raise ValueError('阵容 JSON 格式无效')
        documents = raw['lineups']
    else:
        documents = list(raw.values())
    result = {}
    for document in documents:
        if 'slots' not in document:
            document = _legacy_document(document)
        model = CustomLineup.model_validate(document)
        if model.key in result:
            raise ValueError(f'重复阵容标识：{model.key}')
        result[model.key] = model.model_dump()
    return result


def write_collection(path, documents):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 2, 'lineups': list(documents.values())},
                                   ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def migrate_collection(path):
    path = Path(path)
    if not path.exists():
        return
    with FileLock(str(path) + '.lock'):
        raw = json.loads(path.read_text(encoding='utf-8'))
        if raw.get('version') == 2:
            return
        backup = path.with_suffix('.v1.bak.json')
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        write_collection(path, read_collection(path))


def read_lineups():
    migrate_collection(STORE)
    return read_collection(STORE)


def save_lineup(lineup, path=None, overwrite=True):
    path = Path(path or STORE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock'):
        data = read_collection(path)
        if not overwrite and lineup.key in data:
            raise FileExistsError(lineup.key)
        data[lineup.key] = lineup.model_dump()
        write_collection(path, data)
    return data[lineup.key]


def delete_lineup(key, path=None):
    path = Path(path or STORE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock'):
        data = read_collection(path)
        if key not in data:
            raise KeyError(key)
        del data[key]
        write_collection(path, data)


def load_strategy(key):
    raw = read_lineups().get(key)
    if raw is None:
        raise ValueError(f'自定义阵容不存在：{key}；请先在 chesslineup 中保存')
    return CustomLineup.model_validate(raw).strategy()
