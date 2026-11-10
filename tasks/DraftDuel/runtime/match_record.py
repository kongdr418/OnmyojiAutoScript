"""Optional local recorder for one complete co-op run.

A run is one 6-win / 2-loss series, not one individual battle.  The recorder
is intentionally local and passive: it never changes a draft or deployment
decision, and it is disabled unless the user enables ``save_real_match``.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from module.logger import logger

LOG_DIR = Path(__file__).resolve().parents[3] / 'log' / 'DraftDuel' / 'real_matches'


class RealMatchRecorder:
    def __init__(self, role: str):
        self.role = role
        self.data = {
            'schema_version': 1,
            'role': role,
            'started_at': datetime.now().isoformat(timespec='seconds'),
            'draft': {'own': [], 'mate': [], 'rounds': []},
            'battles': [],
            'result': {},
        }
        self.saved = False

    def record_draft_pick(self, index, offers, chosen):
        self.data['draft']['rounds'].append({
            'pick': int(index),
            'offers': list(offers or []),
            'chosen': chosen,
        })

    def record_draft(self, own, mate=None):
        self.data['draft']['own'] = list(own or [])
        self.data['draft']['mate'] = list(mate or [])

    def record_battle_plan(self, own_plan, mate_plan=None):
        self.data['battles'].append({
            'own_plan': list(own_plan or []),
            'mate_plan': list(mate_plan or []),
            'auto': False,
            'result': None,
        })

    def record_auto_battle(self):
        self.data['battles'].append({
            'own_plan': [],
            'mate_plan': [],
            'auto': True,
            'result': None,
        })

    def record_battle_result(self, result: str):
        if result not in ('win', 'loss'):
            return
        if not self.data['battles'] or self.data['battles'][-1]['result'] is not None:
            self.data['battles'].append({
                'own_plan': [],
                'mate_plan': [],
                'result': result,
            })
        else:
            self.data['battles'][-1]['result'] = result

    def save(self, wins: int, losses: int):
        if self.saved:
            return None
        self.saved = True
        self.data['result'] = {
            'wins': int(wins),
            'losses': int(losses),
            'fan': int(wins),
            'finished_at': datetime.now().isoformat(timespec='seconds'),
            'elapsed_seconds': round(
                time.time() - datetime.fromisoformat(self.data['started_at']).timestamp(), 1
            ),
        }
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            path = LOG_DIR / f'{stamp}-{self.role}.json'
            suffix = 1
            while path.exists():
                path = LOG_DIR / f'{stamp}-{self.role}-{suffix}.json'
                suffix += 1
            path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.info(f'DraftDuel: real match saved {path}')
            return path
        except Exception as exc:
            logger.warning(f'DraftDuel: real match save failed: {exc}')
            return None
