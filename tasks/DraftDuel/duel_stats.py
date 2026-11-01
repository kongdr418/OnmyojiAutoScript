"""协同对弈实战统计：记录每场我方阵容与胜负，用于选人/上阵加权。

数据结构:
{
  "battles": [
    {"ts": 1730000000, "result": "win",
     "ours": [5个初选], "fielded": [3个上阵],
     "mate": [队友5个], "opponent": []}
  ]
}
对手式神名只能来自战报(仅画像)，暂时留空，等画像库建好后补。
"""

import json
import time
from pathlib import Path

STATS_PATH = (Path(__file__).resolve().parents[2]
              / 'assets' / 'DraftDuel' / 'duel_stats.json')
MIN_SAMPLES = 3
MAX_BONUS = 8.0


def load() -> dict:
    try:
        return json.loads(STATS_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'battles': []}


def record(result, ours=(), fielded=(), mate=(), opponent=()) -> dict:
    data = load()
    entry = {
        'ts': round(time.time()),
        'result': result,
        'ours': list(ours),
        'fielded': list(fielded),
        'mate': list(mate),
        'opponent': list(opponent),
    }
    battles = data.setdefault('battles', [])
    battles.append(entry)
    data['battles'] = battles[-500:]
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding='utf-8')
    except Exception:
        pass
    return entry


def _tally(key='ours'):
    table = {}
    for battle in load().get('battles', []):
        result = battle.get('result')
        if result not in ('win', 'loss'):
            continue
        for name in battle.get(key) or []:
            if not name:
                continue
            win, loss = table.get(name, (0, 0))
            table[name] = (win + (result == 'win'), loss + (result == 'loss'))
    return table


def pick_bonus(name: str, table=None) -> float:
    """按实战胜率给选人加分; 样本不足不加."""
    table = table if table is not None else _tally('ours')
    win, loss = table.get(name, (0, 0))
    if win + loss < MIN_SAMPLES:
        return 0.0
    rate = win / (win + loss)
    return max(-MAX_BONUS, min(MAX_BONUS, (rate - 0.5) * 2 * MAX_BONUS))


def field_bonus(name: str, table=None) -> float:
    """按"上阵时"的胜率给上阵加分; 样本不足不加."""
    table = table if table is not None else _tally('fielded')
    win, loss = table.get(name, (0, 0))
    if win + loss < MIN_SAMPLES:
        return 0.0
    rate = win / (win + loss)
    return max(-MAX_BONUS, min(MAX_BONUS, (rate - 0.5) * 2 * MAX_BONUS))


def summary_line(table) -> str:
    if not table:
        return '无实战样本'
    rows = sorted(((w + l, w, l, name) for name, (w, l) in table.items()), reverse=True)
    parts = [f'{name} {w}胜{l}负' for _, w, l, name in rows[:5]]
    return ', '.join(parts)
