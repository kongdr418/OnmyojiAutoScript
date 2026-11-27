"""Map the full-frame detector to the OAS agent and its existing priority labels."""
from oashya.labels import buff, g, n, r, sp, sr, ssr
from .tracker_labels import MODEL_LABELS


# Keep desktop priority labels (e.g. ssr_001) while adding the supplied model's classes.
_groups = {
    'buff': dict(buff), 'n': dict(n), 'g': dict(g), 'r': dict(r),
    'sr': dict(sr), 'ssr': dict(ssr), 'sp': dict(sp), 'initial': {},
}
_aliases = {
    '女鬼红叶': '鬼女红叶', '镰鼬': '镰铀', '鸩': '档鸩', '饴细工': '怡细工',
    '玉藻前': '玉澡前', '饭笥': '饭筒', '伊邪娜美': '伊邪那美', '烬天玉藻前': '烬天玉澡前',
}
_buff_codes = {
    '豆子获取': 'buff_004', '加速撒豆': 'buff_003', '减速': 'buff_002',
    'UP': 'buff_006', '好友UP': 'buff_007', '冰冻': 'buff_005',
}
_boss_scores = {'n': 1, 'r': 2, 'sr': 3, 'ssr': 4, 'sp': 5, 'ur': 6}
_codes_by_name = {
    (tier, name): code for tier, items in _groups.items() for code, name in items.items()
}
_model_codes = []
_boss_codes = {}

for model_id, label in enumerate(MODEL_LABELS):
    tier, name = label.split('-', 1)
    if tier == 'error':
        code = None
    elif tier == 'BUFF':
        code = _buff_codes[name]
    elif tier == '初始':
        code = f'initial_{name}'
        _groups['initial'][code] = label
        _boss_codes[code] = _boss_scores[name]
    else:
        group = 'sp' if tier == 'ur' else tier
        code = _codes_by_name.get((group, _aliases.get(name, name)))
        if code is None:
            code = f'{tier}_model_{model_id}'
            _groups[group][code] = name
        if tier == 'ur':
            _boss_codes[code] = _boss_scores['ur']
    _model_codes.append(code)

CLASSIFY = []
_ranges = {}
for _tier, _items in _groups.items():
    first = len(CLASSIFY)
    for _code, _name in _items.items():
        CLASSIFY.append({'name': _name, 'class': _code, 'id': len(CLASSIFY)})
    _ranges[_tier] = (first, len(CLASSIFY) - 1)

_ids = {item['class']: item['id'] for item in CLASSIFY}
MODEL_CLASS_IDS = tuple(_ids.get(code) for code in _model_codes)
MODEL_BOSS_SCORES = {_ids[code]: score for code, score in _boss_codes.items()}
INITIAL_IDS = frozenset(_ids[code] for code in _groups['initial'])
_model_aliases = dict(zip(MODEL_LABELS, MODEL_CLASS_IDS))


class CLASSINDEX:
    MIN_BUFF, MAX_BUFF = _ranges['buff']
    MIN_N, MAX_N = _ranges['n']
    MIN_G, MAX_G = _ranges['g']
    MIN_R, MAX_R = _ranges['r']
    MIN_SR, MAX_SR = _ranges['sr']
    MIN_SSR, MAX_SSR = _ranges['ssr']
    MIN_SP, MAX_SP = _ranges['sp']
    BUFF_001 = _ids['buff_001']
    BUFF_002 = _ids['buff_002']
    # Match the buff weights used in the desktop agent's omega_buff switch.
    BUFF_003 = _ids['buff_004']  # Add beans
    BUFF_004 = _ids['buff_006']  # Probability up
    BUFF_005 = _ids['buff_005']
    BUFF_006 = _ids['buff_003']  # Faster throws
    BUFF_007 = _ids['buff_007']
    R_007 = _ids['r_007']
    R_008 = _ids['r_008']


def id2label(idd: int) -> str:
    return CLASSIFY[idd]['class']


def id2name(idd: int) -> str:
    return CLASSIFY[idd]['name']


def label2id(label: str) -> int:
    if label in _ids:
        return _ids[label]
    result = _model_aliases.get(label)
    if result is None:
        raise ValueError(label)
    return result
