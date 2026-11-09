"""Local three-plus model inference for DraftDuel.

The model and its name catalog live inside the OAS project.  There is no
external worker, no absolute path and no network dependency.  Any missing
name/model error returns None so the normal OAS strategy remains the fallback.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

from module.logger import logger
from tasks.DraftDuel.matrix import ScoringMatrix, legal_lineup
from tasks.DraftDuel.strategy import rank_picks

MODEL_PATH = Path(__file__).resolve().parents[1] / 'models' / 'recommended-model.json'
TAGS = ('fire', 'damage', 'sustain', 'shield', 'control', 'tempo', 'support')
_MODEL = None
_NAME_TO_ID = None
_ID_TO_INDEX = None
_HERO_IDS = None
_CATALOG = None
_PAIRS = {}


def _load():
    global _MODEL, _NAME_TO_ID, _ID_TO_INDEX, _HERO_IDS, _CATALOG
    if _MODEL is not None:
        return _MODEL
    with MODEL_PATH.open(encoding='utf-8') as handle:
        model = json.load(handle)
    if model.get('observation') != 'names_only_v2' or model.get('dimensions') != 430:
        raise ValueError('unsupported model contract')
    _MODEL = model
    _CATALOG = model['name_catalog']
    _NAME_TO_ID = {value['name']: int(key) for key, value in _CATALOG.items()}
    _HERO_IDS = sorted(_NAME_TO_ID.values())
    _ID_TO_INDEX = {hid: index for index, hid in enumerate(_HERO_IDS)}
    return model


def _seed(*parts):
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return int(hashlib.sha256(payload.encode()).hexdigest()[:8], 16)


def _features(cards, context=(), ordered=False):
    model = _load()
    size = len(cards)
    if not size:
        raise ValueError('empty cards')
    vector = [0.] * model['dimensions']
    offset = len(_HERO_IDS)
    for slot, name in enumerate(cards):
        hid = _NAME_TO_ID[name]
        tags = _CATALOG[str(hid)]['tags']
        vector[_ID_TO_INDEX[hid]] += 1 / size
        for index, tag in enumerate(TAGS):
            vector[offset + index] += float(tag in tags) / size
        peers = [peer for peer in (*cards, *context) if peer != name]
        for peer in peers:
            pair = tuple(sorted((hid, _NAME_TO_ID[peer])))
            if pair not in _PAIRS:
                _PAIRS[pair] = _seed('names-pair', pair) % 128
            vector[offset + len(TAGS) + _PAIRS[pair]] += 1 / (size * max(1, len(peers)))
        if ordered:
            for index, tag in enumerate(TAGS):
                vector[offset + len(TAGS) + 128 + 4 + slot * len(TAGS) + index] = float(tag in tags)
            vector[offset + len(TAGS) + 128 + 4 + len(TAGS) * 3 + _seed('names-position', slot, hid) % 32] += 1 / size
    for index, tag in enumerate(('fire', 'damage', 'sustain', 'control')):
        count = sum(tag in _CATALOG[str(_NAME_TO_ID[name])]['tags'] for name in (*cards, *context))
        vector[offset + len(TAGS) + 128 + index] = min(count, 3) / 3
    return vector


def _dot(weights, vector):
    return sum(weight * value for weight, value in zip(weights, vector))


def recommend_pick(role, offers, own, mate):
    try:
        _load()
        if role not in ('captain', 'member'):
            return None
        offers = [name for name in offers if name in _NAME_TO_ID]
        own = [name for name in own if name in _NAME_TO_ID]
        mate = [name for name in mate if name in _NAME_TO_ID]
        if not offers:
            return None
        ranked = rank_picks(offers, own, mate=mate, team_size=5)
        best = None
        for pick in ranked:
            value = pick.score + 100 * _dot(_MODEL['weights']['draft'], _features([pick.name], own + mate))
            if best is None or value > best[0]:
                best = (value, pick.name)
        if best is None:
            return None
        logger.info(f'DraftDuel: local model pick role={role} choice={best[1]}')
        return best[1]
    except Exception as exc:
        logger.warning(f'DraftDuel: local model pick failed: {exc}')
        return None


def recommend_lineup(own, mate):
    try:
        _load()
        own = [name for name in own if name in _NAME_TO_ID]
        mate = [name for name in mate if name in _NAME_TO_ID]
        if len(own) < 3 or len(mate) < 3:
            return None
        matrix = ScoringMatrix((*own, *mate))
        choices = []
        for left in itertools.combinations(sorted(own), 3):
            for right in itertools.combinations(sorted(mate), 3):
                if not legal_lineup((*left, *right)):
                    continue
                prior = matrix.score((*left, *right)).total
                value = prior + 100 * _dot(_MODEL['weights']['deploy'], _features([*left, *right]))
                choices.append((value, left, right))
        if not choices:
            return None
        _, left, right = max(choices, key=lambda item: item[0])
        result = {}
        for role, names, other in (('captain', left, right), ('member', right, left)):
            orders = list(itertools.permutations(names))
            scored = [(100 * _dot(_MODEL['weights']['order'], _features(list(order), other, ordered=True)), order)
                      for order in orders]
            result[role] = list(max(scored, key=lambda item: item[0])[1])
        logger.info(f'DraftDuel: local model lineup {result}')
        return result
    except Exception as exc:
        logger.warning(f'DraftDuel: local model lineup failed: {exc}')
        return None
