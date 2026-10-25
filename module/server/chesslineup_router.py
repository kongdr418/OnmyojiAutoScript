"""Local Chess lineup editor, hosted alongside annotator."""
import re
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tasks.Chess.strategy.custom_lineups import (
    BONDS, EMBLEMS, ROOT, CustomLineup, read_lineups, save_lineup,
    read_collection, delete_lineup,
)
from tasks.Chess.strategy.lineup_loader import LINEUPS_FILE
from tasks.Chess.strategy.shikigami_catalog import SHIKIGAMI_ENTRIES
from tasks.Chess.strategy.soul_catalog import SOUL_ENTRIES

chesslineup_app = APIRouter(prefix='/tool/chesslineup', tags=['chesslineup'])
WEB = Path(__file__).resolve().parent / 'web/chesslineup'


@chesslineup_app.get('')
async def editor():
    return FileResponse(WEB / 'index.html')


@chesslineup_app.get('/api/catalog')
async def catalog():
    def image(kind, key):
        return f'/tool/chesslineup/static/cards/{kind}_{key}.webp'
    return {
        'shikigami': [{'key': e.romaji, 'name': e.chinese_name, 'cost': e.cost,
                       'bonds': list(e.bonds), 'image': image('shikigami', e.romaji)} for e in SHIKIGAMI_ENTRIES],
        'soul': [{'key': e.romaji, 'name': e.chinese_name, 'category': e.category,
                  'image': image('soul', e.romaji)} for e in SOUL_ENTRIES],
        'emblem': [{'key': key, 'name': e['name'], 'image': image('emblem', key)} for key, e in EMBLEMS.items()],
        'bonds': BONDS,
        'configs': sorted(p.stem for p in (ROOT / 'config').glob('*.json') if p.stem != 'template'),
    }


@chesslineup_app.get('/api/lineups')
async def lineups():
    return {'builtin': list(read_collection(LINEUPS_FILE).values()),
            'custom': list(read_lineups().values())}


@chesslineup_app.post('/api/lineups')
async def save(data: CustomLineup, source: Literal['builtin', 'custom'] = 'custom'):
    return save_lineup(data, path=LINEUPS_FILE if source == 'builtin' else None)


@chesslineup_app.post('/api/lineups/promote')
async def promote(data: CustomLineup):
    try:
        return save_lineup(data, path=LINEUPS_FILE, overwrite=False)
    except FileExistsError:
        raise HTTPException(409, '内置阵容中已存在相同标识，请先另存为并修改标识后再转换')


@chesslineup_app.delete('/api/lineups/{source}/{key}')
async def remove(source: Literal['builtin', 'custom'], key: str):
    from tasks.Chess.strategy.lineup import LEGACY_LINEUP_NAMES
    references = []
    for path in (ROOT / 'config').glob('*.json'):
        if path.stem == 'template':
            continue
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        chess = raw.get('chess', {})
        selected = chess.get('chess_config', {})
        bond = selected.get('lineup_bond', '七角山' if chess.get('scheduler', {}).get('enable') else None)
        active_key = selected.get('custom_lineup', '')
        if bond in ('海国', 'haiguo') or active_key == 'builtin:haiguo':
            reference = 'custom:haiguo'
        elif bond == '自定义阵容':
            reference = active_key if active_key.startswith(('builtin:', 'custom:')) else 'custom:' + active_key
        else:
            reference = 'builtin:' + next((name for name, label in LEGACY_LINEUP_NAMES.items()
                                           if bond in (name, label)), str(bond or ''))
        if reference == f'{source}:{key}':
            references.append(path.stem)
    if references:
        raise HTTPException(409, '阵容仍被以下脚本使用，请先切换：' + '、'.join(references))
    try:
        delete_lineup(key, path=LINEUPS_FILE if source == 'builtin' else None)
    except KeyError:
        raise HTTPException(404, '阵容不存在')
    return {'message': '阵容已删除'}


class ApplyBody(BaseModel):
    key: str
    script: str
    source: Literal['builtin', 'custom'] = 'custom'


@chesslineup_app.post('/api/apply')
async def apply(data: ApplyBody):
    documents = read_collection(LINEUPS_FILE) if data.source == 'builtin' else read_lineups()
    if data.key not in documents:
        raise HTTPException(404, '请先保存阵容')
    if not re.fullmatch(r'[\w -]{1,80}', data.script) or data.script == 'template':
        raise HTTPException(400, '脚本配置名无效')
    if not (ROOT / 'config' / f'{data.script}.json').is_file():
        raise HTTPException(404, '脚本配置不存在')
    from module.server.main_manager import mm
    from tasks.Chess.strategy.lineup import LineupBond
    config = mm.config_cache(data.script)
    config.model.chess.chess_config.custom_lineup = f'{data.source}:{data.key}'
    config.model.chess.chess_config.lineup_bond = LineupBond.CUSTOM
    config.model.save()
    return {'message': f'已应用至 {data.script}，下一局或下次启动百鬼棋局时使用'}
