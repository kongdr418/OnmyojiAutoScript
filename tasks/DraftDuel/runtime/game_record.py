"""Read the game's own pvp2v2_record and select the latest run by time.

The game keeps one ``pvp2v2_record`` under each account's Documents/userdata
folder.  OAS does not need to know which account is active: it finds all
records on the device, parses their run start times, and picks the run that
overlaps the current DraftDuel run.  The parser is read-only and uses ADB
only to locate/pull the file.
"""
from __future__ import annotations

import json
import struct
import time
from pathlib import Path

from module.logger import logger


def _all(data: bytes, needle: bytes):
    out = []
    pos = 0
    while True:
        pos = data.find(needle, pos)
        if pos < 0:
            return out
        out.append(pos)
        pos += 1


def _i64(data: bytes, pos: int):
    if pos < 0 or pos + 8 > len(data):
        return None
    return struct.unpack_from('<q', data, pos)[0]


def _hero_ids(data: bytes, start: int, end: int):
    return [struct.unpack_from('<Q', data, p + 7)[0]
            for p in _all(data, b'heroId\x00') if start <= p < end]


def parse_bytes(data: bytes):
    """Return parsed battles plus runs grouped by the friendly six heroes."""
    camps = [p for p in _all(data, b'camp\x00') if p + 13 <= len(data)]
    battles = []
    for i, pos in enumerate(camps):
        end = camps[i + 1] if i + 1 < len(camps) else len(data)
        start_pos = data.rfind(b'start_time\x00', 0, pos)
        started = _i64(data, start_pos + 11) if start_pos >= 0 else None
        is_win_pos = data.find(b'is_win\x00', pos, end)
        is_win = bool(data[is_win_pos + 7]) if is_win_pos >= 0 else None
        enemy_pos = data.find(b'enemy_list\x00', pos, end)
        friend_pos = data.find(b'frend_list\x00', enemy_pos if enemy_pos >= 0 else pos, end)
        team_pos = data.find(b'team_id\x00', friend_pos if friend_pos >= 0 else pos, end)
        enemy = _hero_ids(data, enemy_pos, friend_pos) if enemy_pos >= 0 else []
        friend = _hero_ids(data, friend_pos, team_pos if team_pos >= 0 else end) if friend_pos >= 0 else []
        battles.append({
            'started_at': started,
            'camp': _i64(data, pos + 5),
            'is_win': is_win,
            'enemy_hero_ids': enemy[:6],
            'friend_hero_ids': friend[:6],
        })
    runs = []
    for battle in battles:
        key = tuple(battle['friend_hero_ids'])
        if not runs or runs[-1]['friend_hero_ids'] != list(key):
            runs.append({'friend_hero_ids': list(key), 'battles': [], 'wins': 0})
        runs[-1]['battles'].append(battle)
        runs[-1]['wins'] += int(bool(battle['is_win']))
    for run in runs:
        run['losses'] = len(run['battles']) - run['wins']
        run['fan'] = run['wins']
        run['started_at'] = next((b['started_at'] for b in run['battles']
                                  if b['started_at']), None)
        run['finished_at'] = max((b['started_at'] for b in run['battles']
                                  if b['started_at']), default=None)
    return battles, runs


class GameRecordReader:
    def __init__(self, device, package: str | None = None):
        self.device = device
        self.package = package or getattr(device, 'package', None)

    def _paths(self):
        if not self.package:
            return []
        base = f'/sdcard/Android/data/{self.package}/files/netease/onmyoji/Documents/userdata'
        try:
            out = self.device.adb_shell(['find', base, '-name', 'pvp2v2_record', '-type', 'f'])
        except Exception as exc:
            logger.warning(f'DraftDuel: game record find failed: {exc}')
            return []
        return [line.strip() for line in str(out or '').splitlines()
                if line.strip().endswith('pvp2v2_record')]

    def latest_run(self, run_started_at: float, run_finished_at: float | None = None):
        finished = run_finished_at or time.time()
        candidates = []
        for remote in self._paths():
            local = Path('./log/DraftDuel/game_record_tmp') / str(abs(hash(remote)))
            try:
                local.parent.mkdir(parents=True, exist_ok=True)
                self.device.adb_command(['pull', remote, str(local)], timeout=30)
                data = local.read_bytes()
                _, runs = parse_bytes(data)
                for run in runs:
                    started = run.get('started_at')
                    if started and run_started_at - 300 <= started <= finished + 300:
                        candidates.append((started, remote, run))
            except Exception as exc:
                logger.warning(f'DraftDuel: game record read failed {remote}: {exc}')
                continue
            finally:
                try:
                    local.unlink(missing_ok=True)
                except Exception:
                    pass
        if not candidates:
            return None
        _, remote, run = max(candidates, key=lambda item: item[0])
        return {'source': remote, 'run': run}


def _same_run(left, right):
    if not left or not right:
        return False
    return (left.get('started_at') == right.get('started_at')
            and left.get('friend_hero_ids') == right.get('friend_hero_ids'))


def save_run(found, role: str, run_started_at: float, draft=None):
    """Write one game run; merge duplicate captain/member saves by run identity."""
    path = Path('./log/DraftDuel/real_matches')
    path.mkdir(parents=True, exist_ok=True)
    run = found['run']
    for existing in path.glob('*-game*.json'):
        try:
            payload = json.loads(existing.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not _same_run(payload.get('run'), run):
            continue
        roles = list(payload.get('roles') or [payload.get('role')])
        if role not in roles:
            roles.append(role)
            payload['roles'] = roles
        drafts = payload.setdefault('drafts', {})
        if not drafts and payload.get('draft') and payload.get('role'):
            drafts[payload['role']] = payload['draft']
        if draft:
            drafts[role] = draft
            if not payload.get('draft'):
                payload['draft'] = draft
        existing.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info(f'DraftDuel: duplicate game run merged into {existing}')
        return existing
    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = path / f'{stamp}-{role}-game.json'
    suffix = 1
    while out.exists():
        out = path / f'{stamp}-{role}-game-{suffix}.json'
        suffix += 1
    payload = {
        'schema_version': 1,
        'source': 'pvp2v2_record',
        'source_path': found['source'],
        'role': role,
        'roles': [role],
        'requested_run_started_at': run_started_at,
        'draft': draft or {'own': [], 'mate': [], 'rounds': []},
        'drafts': {role: draft} if draft else {},
        'run': run,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f'DraftDuel: game record saved {out}')
    return out
