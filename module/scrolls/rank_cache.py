"""Keep timestamped rankings scoped to the exact server and current issue."""
from datetime import datetime
import json
import math

from filelock import FileLock

from module.scrolls import storage


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _candidate(row, captured_at, source):
    stamp = row.get('rank_queried_at') or captured_at
    if not _timestamp(stamp):
        return None
    values = [row.get('rank_20_score'), row.get('rank_100_score')]
    if all(value is None for value in values):
        return None
    if any(value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)
           for value in values):
        return None
    if all(value is not None for value in values) and values[0] < values[1]:
        return None
    rankings = row.get('rankings', [])
    if not isinstance(rankings, list) or len(rankings) > 100:
        return None
    scores = []
    for index, entry in enumerate(rankings, 1):
        if not isinstance(entry, dict) or entry.get('rank') != index:
            return None
        score = entry.get('score')
        if type(score) not in (int, float) or not math.isfinite(score) or score < 0:
            return None
        scores.append(score)
    if scores != sorted(scores, reverse=True):
        return None
    for position, value in zip((20, 100), values):
        if rankings and value is not None and (len(scores) < position or scores[position - 1] != value):
            return None
    return {'rank_20_score': values[0], 'rank_100_score': values[1], 'rankings': rankings,
            'rank_queried_at': stamp, 'rank_original_source': row.get('rank_original_source') or source}


def _ingest(saved, result, server_id, activity):
    if (not isinstance(result, dict) or str(result.get('identity', {}).get('server_id')) != server_id
            or not activity.matches(result)):
        return
    for row in result.get('scrolls', []):
        painting = str(row.get('painting_id', ''))
        if painting not in activity.painting_ids:
            continue
        candidate = _candidate(row, result.get('queried_at'),
                               row.get('rank_source') or ('server' if result.get('source') == 'independent' else 'game_cache'))
        if candidate and (painting not in saved or
                          _timestamp(candidate['rank_queried_at']) > _timestamp(saved[painting]['rank_queried_at'])):
            saved[painting] = candidate


def enrich_rankings(result, activity):
    """Never replace cached collection times with the current progress time."""
    server_id = str(result.get('identity', {}).get('server_id', ''))
    if not server_id.isdigit() or not activity.matches(result):
        return result
    key = storage.storage_key(server_id + '\n' + activity.fingerprint)
    path = storage.ROOT / 'rankings' / (key + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path.with_suffix('.lock'))).acquire(timeout=5):
        saved = {}
        if path.exists():
            try:
                if path.stat().st_size <= 512 * 1024:
                    previous = json.loads(path.read_text('utf-8'))
                    _ingest(saved, previous, server_id, activity)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
        # Migrate same-issue results that predate the shared ranking cache.
        if not saved:
            for legacy in storage.ROOT.glob('*.json'):
                try:
                    if legacy.stat().st_size <= 2 * 1024**2:
                        _ingest(saved, json.loads(legacy.read_text('utf-8')), server_id, activity)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue
        _ingest(saved, result, server_id, activity)
        for row in result.get('scrolls', []):
            painting = str(row.get('painting_id', ''))
            cached = saved.get(painting)
            incoming = _candidate(row, result.get('queried_at'),
                                  row.get('rank_source') or ('server' if result.get('source') == 'independent' else 'game_cache'))
            if not cached:
                continue
            use_cache = incoming is None or _timestamp(cached['rank_queried_at']) > _timestamp(incoming['rank_queried_at'])
            if use_cache:
                row.update(cached)
                row['rank_source'] = 'cache'
            else:
                row.update(incoming)
                row['rank_source'] = row.get('rank_source') or ('server' if result.get('source') == 'independent' else 'game_cache')
        stored = {'identity': {'server_id': server_id}, 'activity': activity.metadata(),
                  'scrolls': [dict(painting_id=painting, **row) for painting, row in saved.items()]}
        storage.atomic_write(path, json.dumps(stored, ensure_ascii=False).encode('utf-8'))
    if all(row.get('rank_20_score') is not None and row.get('rank_100_score') is not None
           for row in result.get('scrolls', [])):
        result['warnings'] = [warning for warning in result.get('warnings', [])
                              if '本次内存读取未取得排行榜' not in warning]
    return result
