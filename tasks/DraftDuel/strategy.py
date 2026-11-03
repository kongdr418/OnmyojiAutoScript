"""从《协同分析.xlsx》生成的资料中选择式神和推荐搭配。

本模块不依赖游戏画面。调用方传入当前可选式神及已选队伍，
仅在 OCR 能可靠识别名称后才调用 choose_pick。
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


try:
    from tasks.DraftDuel import duel_stats
except Exception:  # 离线单文件测试时允许缺统计模块
    duel_stats = None

DATA_DIR = Path(__file__).resolve().parents[2] / 'assets' / 'DraftDuel'
CATALOG_PATH = DATA_DIR / 'catalog.json'
ENCYCLOPEDIA_PATH = DATA_DIR / 'encyclopedia.json'
STRENGTH_PATH = DATA_DIR / 'strength_scores.json'
TIER_POINTS = {'特等': 40, '上等': 30, '中等': 20, '下等': 10}
ENCYCLOPEDIA_POINTS = {'SS': 12, 'S': 8, 'A': 4, 'B': 0,
                       'C': -4, 'D': -8, 'E': -12}
USER_RATINGS = {
    '梦山白藏主': 'S',
    '灼华桃花妖': 'S',
    '蚀月吸血姬': 'S',
    '天火命铃彦姬': 'S',
    # 用户钦定（2026-10-28）：这些卡在百科里要么样本极少要么评级偏低, 按强卡处理
    '卑弥呼': 'S',
    '平将门': 'S',
    '云间不见岳': 'S',
    # 用户追加（2026-10-28）：莹草/化鲸已加强、SSR鬼童丸也是强单卡, 百科胜率是旧数据
    '莹草': 'S',
    '化鲸': 'S',
    '鬼童丸': 'S',
    # 用户追加（2026-10-29）：茨木童子也加强了, 输出评分要抬上来
    '茨木童子': 'S',
}
USER_TIERS = {'莹草': '特等', '云间不见岳': '特等',
              '卑弥呼': '特等', '平将门': '特等',
              # 六番阵容里各出现 4 套的常驻火机/辅助（百科无样本, 按实战略高定档）
              '思金神': '特等', '神无月': '特等',
              '鬼童丸': '特等', '茨木童子': '特等'}
SUMMON_SLOT_USERS = frozenset({'猫川', '千姬', '雪御前', '跳跳妹妹', '惠比寿'})
USER_ROLES = {
    '梦山白藏主': {'support'},
    '灼华桃花妖': {'sustain'},
    '蚀月吸血姬': {'sustain', 'damage'},
    '天火命铃彦姬': {'damage'},
    # 用户口径(2026-10-30): 初音未来不是鬼火, 是加速+增伤(带一丝丝盾)
    '初音未来': {'support', 'tempo'},
    # 百科/官方口径：卑弥呼是"拉条+增伤+回合外行动"的多功能辅助（119 初始速度），
    # 不是主输出；协同里她负责拉条/增伤，输出交给茨林、SP泷夜叉姬、平将门等。
    '卑弥呼': {'support', 'tempo'},
    # 木之本樱：被动"幻影"控制 + 全队无视防御，控制兼增伤。
    '木之本樱': {'control', 'support'},
    # 莹草：反击伤害加强后是"奶 + 输出"混合位, 不能只当纯奶统计。
    '莹草': {'sustain', 'damage'},
    # SP惠比寿（晨辉惠比寿）：推条 + 回血 + 回火，用户认定是优质辅助卡，
    # 三个位（打火/治疗/拉条）都能顶。
    '晨辉惠比寿': {'fire', 'sustain', 'tempo'},
}
ROLE_POINTS = {'fire': 12, 'sustain': 10, 'shield': 7, 'control': 6,
               'damage': 8, 'tempo': 6, 'support': 4}
# 2026-10-28 选人页实测的整名误认（描边字体 + 漏字），逐条对应图鉴规范名。
# 这些名字无法靠单字替换或近形相似度唯一还原，只能整名映射。
OCR_NAME_ALIASES = {
    '久次食良': '久次良',
    '书羽': '书翁',
    '其佣': '兵勇',
    '夜又': '夜叉',
    '孟婴': '孟婆',
    '巷风一百连': '苍风一目连',
    '一百连': '一目连',
    '曰藏王': '白藏主',
    '梦山曰藏王': '梦山白藏主',
    '炼狱茨术量子': '炼狱茨木童子',
    '神心会外镜': '禅心云外镜',
    '蛋草': '莹草',
    '萤草': '莹草',
    '酒谷量子': '酒吞童子',
    '饭司': '饭笥',
    '饭筒': '饭笥',
    '鲸沙干姬': '鲸汐千姬',
    '鸠': '鸩',
    '李': '李小狼',
    '权图': '椒图',
    '泥夜文姬': '泷夜叉姬',
    # 2026-10-29 选人页实测
    '神塑八岐天蛇': '神堕八岐大蛇',
    '神塑八岐大蛇': '神堕八岐大蛇',
    '大夜摩大阎魔': '大夜摩天阎魔',
    '柄洋种': '祸津神',
    '鲸炒干姬': '鲸汐千姬',
    '披岸化': '彼岸花',
    # 用户确认：心发天神 = 心友犬神
    '心发天神': '心友犬神',
}
# 描边字体下 OCR 的常见近形误认; 仅在纠正后能命中图鉴时才采用
OCR_CHAR_FIX = {
    '乌': '鸟', '蛛': '蝶', '巻': '卷', '稱': '祢', '煉': '炼',
    '姫': '姬', '鈴': '铃', '鳥': '鸟', '龍': '龙', '鳳': '凤',
    '剣': '剑', '歩': '步', '壱': '壹', '畫': '画', '黃': '黄',
    '黒': '黑', '澤': '泽', '実': '实', '涼': '凉', '鍊': '炼',
    # 实测 2026-10-28: 弈→升, 帝释天→带释天
    '升': '弈', '带': '帝',
    # 实测 2026-10-28 20:31 选人: 茨木童子→次木量子, 待宵姑获鸟→待肖姑获马
    '次': '茨', '量': '童', '术': '木', '肖': '宵', '马': '鸟',
}
_OCR_FIX_TABLE = str.maketrans(OCR_CHAR_FIX)
SOUL_NAMES = (
    '伤魂鸟', '轮入道', '青女房', '返魂香', '雪幽魂', '钓瓶火',
    '遗念火', '招财', '火灵', '蚌精', '木魅', '薙魂', '地藏',
    '针女', '破势', '狂骨', '网切', '树妖', '珍珠', '狰',
    '魅妖', '魍魉', '钟灵', '日女', '共潜', '共浅', '镜姬',
    '贝吹坊', '出世螺', '隐念', '海月', '三味', '镇墓兽',
)

# 编排公式（用户口径，6 人合计）：2 打火 + 2 输出 + 1 治疗/盾 + 1 控制或拉条。
# 强力增伤可以顶掉后两个辅助位；评分与上阵都按这个目标算。
FORMULA_ROLES = {'fire': 2, 'damage': 2, 'protect': 1, 'flex': 1}
# 用户口径(2026-10-30): 队伍里至少要 2 个打火, 一手输出一手火不够用。
FIRE_TARGET = 2
# 用户提供的火系名单；与下方供鬼火的 CORE_FIRE 独立。
FLAME_SHIKIGAMI = frozenset({
    '炼狱茨木童子', '烬天玉藻前', '鬼王酒吞童子', '浮世青行灯',
    '缚骨清姬', '大夜摩天阎魔', '心狩鬼女红叶', '福悦座敷童子',
    '云间不见岳', '阎魔', '茨木童子', '青行灯', '玉藻前', '不知火',
    '阿修罗', '食灵', '铃彦姬', '闻人翊悬', '平将门', '思金神',
    '鬼使白', '凤凰火', '清姬', '座敷童子', '狸猫', '武士之灵',
    '古笼火', '天火命铃彦姬', '百羽凤凰火',
})
FLAME_ENABLERS = frozenset({'云间不见岳', '思金神'})


def flame_partners(team: Iterable[str]) -> frozenset[str]:
    """至少有一个核心和另一名火系成员，才构成火系搭配。"""
    members = frozenset(team)
    return (members & FLAME_SHIKIGAMI) - FLAME_ENABLERS if members & FLAME_ENABLERS else frozenset()


# 真正能回火的打火机（用户口径 2026-10-30: 薰也是真火, 能自解控+帮队友驱散还打火）
CORE_FIRE = frozenset({'座敷童子', '辉夜姬', '因幡辉夜姬', '追月神', '流光追月神',
                       '千姬', '鲸汐千姬', '天井下', '薰'})
# 次等打火(有一定回火, 够用但必须配一个真火; 用户口径: sp座敷也算这一档)
SECONDARY_FIRE = frozenset({'鬼金羊', '青行灯', '初音未来', '晨辉惠比寿', '福悦座敷童子'})
# 用户口径(2026-10-30): 这几张不靠鬼火也有强度, 组不到火时凑成"无火队"反而能打
NO_FIRE_CORE = frozenset({'莹草', '化鲸', '荒川之主', '大夜摩天阎魔', '鬼童丸'})
# 用户钦定的高优先级打火: 薰(自解控 + 帮队友驱散 + 回火), 缺火时优先于其它打火机
PRIORITY_FIRE = frozenset({'薰'})
# 用户钦定的强单卡（2026-10-28）：百科样本少或胜率一般也算核心, 不参与 <45% 硬过滤。
USER_STRONG_CARDS = frozenset({'蚀月吸血姬', '莹草', '化鲸', '鬼童丸'})
# 用户钦定的强输出核心：遇到就优先拿, 不受百科胜率门槛限制。
USER_STRONG_DPS = frozenset({'蚀月吸血姬', '平将门', '季', '本真三尾狐',
                             '化鲸', '鬼童丸', '黑童子', '茨木童子'})
# 用户钦定的强组合（名单 + 加分）；能打出六番胜的阵容按强力处理, 不能当弱卡。
USER_COMBOS = (
    (frozenset({'流光追月神', '季', '本真三尾狐'}), 30),
    (frozenset({'流光追月神', '季', '木之本樱'}), 30),
    (frozenset({'流光追月神', '季'}), 16),
    (frozenset({'天照', '黑童子'}), 20),
    (frozenset({'平将门', '云间不见岳'}), 20),
    (frozenset({'纺愿缘结神', '食发鬼'}), 20),
    (frozenset({'鬼王酒吞童子', '禅心云外镜'}), 16),
    # 用户口径(2026-10-30) 强力组合:
    # 初音未来 必须配 sp千姬(鲸汐千姬), 一起上场很强(加速+增伤+一点点盾)
    (frozenset({'初音未来', '鲸汐千姬'}), 22),
    # 不知火 + 酒吞 / sp红叶(心狩鬼女红叶)
    (frozenset({'不知火', '酒吞童子'}), 12),
    (frozenset({'不知火', '心狩鬼女红叶'}), 12),
    # 猫川 + 夏目 / 兵勇, 三者同上再加一点
    (frozenset({'猫川', '夏目猫老师'}), 14),
    (frozenset({'猫川', '兵勇'}), 12),
    (frozenset({'猫川', '夏目猫老师', '兵勇'}), 8),
    # 椒图 + 莹草(两个都算保护, 剩下的人不能再选保护)
    (frozenset({'椒图', '莹草'}), 12),
    # 季 + sp三尾狐(本真三尾狐)
    (frozenset({'季', '本真三尾狐'}), 12),
    # 椒图 / 天照 / 鬼吞 + 小小黑(黑童子)
    (frozenset({'椒图', '黑童子'}), 12),
    (frozenset({'鬼王酒吞童子', '黑童子'}), 12),
    # 云间不见岳 + 平将门/铃彦姬/sp铃彦姬(天火命铃彦姬)/sp座敷(福悦座敷童子)/摩托(大夜摩天阎魔)
    (frozenset({'云间不见岳', '铃彦姬'}), 12),
    (frozenset({'云间不见岳', '天火命铃彦姬'}), 12),
    (frozenset({'云间不见岳', '福悦座敷童子'}), 12),
    (frozenset({'云间不见岳', '大夜摩天阎魔'}), 12),
    # 思金神 + 平将门 / sp铃彦姬
    (frozenset({'思金神', '平将门'}), 12),
    (frozenset({'思金神', '天火命铃彦姬'}), 12),
) + tuple(
    (frozenset({'卑弥呼', partner}), 14)
    for partner in ('炼狱茨木童子', '时曜泷夜叉姬', '平将门',
                    '玉藻前', '烬天玉藻前', '荒川之主')
)
# 强增伤：样本少但用户指定是组合核心（SP不见岳）。
USER_STRONG_SUPPORT = frozenset({'云间不见岳'})
# 用户发来的 12 套实战六番胜阵容（2026-10-28，昵称已按图鉴规范名落库）。
# 昵称对照：sp茶几=时曜泷夜叉姬、sp老头=晨辉惠比寿、老头=惠比寿、小小黑=黑童子、
# p岳/sp不见岳=云间不见岳、sp追=流光追月神、sp桃=灼华桃花妖、sp千姬=鲸汐千姬、
# 美=伊邪那美、离=不知火、平=平将门、因幡=因幡辉夜姬、熏=薰。
SIX_WIN_LINEUPS = (
    frozenset({'化鲸', '天井下', '大天狗', '思金神', '卑弥呼', '因幡辉夜姬'}),
    frozenset({'天火命铃彦姬', '遥念烟烟罗', '日和坊', '思金神', '云间不见岳', '辉夜姬'}),
    frozenset({'莹草', '鬼切', '鲸汐千姬', '追月神', '帝释天', '玉藻前'}),
    frozenset({'思金神', '铃彦姬', '伊邪那美', '神无月', '玉藻前', '云间不见岳'}),
    frozenset({'因幡辉夜姬', '时曜泷夜叉姬', '卑弥呼', '晨辉惠比寿', '鬼金羊', '食发鬼'}),
    frozenset({'薰', '惠比寿', '食发鬼', '神无月', '修罗鬼童丸', '迦楼罗'}),
    frozenset({'蚀月吸血姬', '神无月', '黑童子', '化鲸', '椒图', '不见岳'}),
    frozenset({'茨木童子', '孟婆', '薰', '卑弥呼', '鬼金羊', '神无月'}),
    frozenset({'玉藻前', '云间不见岳', '卑弥呼', '因幡辉夜姬', '孟婆', '日和坊'}),
    frozenset({'鲤鱼精', '灼华桃花妖', '黑童子', '薰', '天照', '流光追月神'}),
    frozenset({'辉夜姬', '言灵', '平将门', '云间不见岳', '思金神', '不知火'}),
    frozenset({'伊邪那美', '莹草', '化鲸', '因幡辉夜姬', '食发鬼', '辉夜姬'}),
)
SIX_WIN_FREQUENCY = Counter(name for lineup in SIX_WIN_LINEUPS for name in lineup)
# 命中白名单阵容的重合人数 -> 加分（3 人起算，6 人全中给满）
SIX_WIN_OVERLAP_POINTS = {3: 8, 4: 18, 5: 32, 6: 45}
# 各类型推荐式神（catalog 规范名；首选在前）。
# 治疗推荐按用户公式：SP桃花妖（灼华桃花妖）、萤草（百科名“莹草”，R卡治疗）、日和坊。
ROLE_RECOMMEND = {
    'fire': ('流光追月神', '薰', '因幡辉夜姬', '神无月', '千姬'),
    'sustain': ('灼华桃花妖', '莹草', '日和坊', '情思日和坊'),
    'control': ('百目鬼', '小松丸'),
    'shield': ('鲤鱼精', '一目连'),
    'damage': ('蚀月吸血姬', '平将门', '时曜泷夜叉姬',
               '炼狱茨木童子', '茨木童子'),
}
RECOMMEND_BONUS = 8      # 首选式神额外加分
RECOMMEND_BONUS_SECOND = 4  # 次选式神额外加分


@dataclass(frozen=True)
class Entry:
    name: str
    rarity: str
    role: str
    strength: str
    weakness: str
    notes: str
    souls: str
    tier: str
    source_row: int


@dataclass(frozen=True)
class Pick:
    name: str
    score: float
    reasons: tuple[str, ...]
    suggested_souls: tuple[str, ...]
    score_parts: tuple[tuple[str, float], ...] = ()
    lineup: tuple[str, ...] = ()


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Entry]:
    data = json.loads(path.read_text(encoding='utf-8'))
    return {row['name']: Entry(**row) for row in data['entries']}


CATALOG = load_catalog()


def load_strength_scores(path: Path = STRENGTH_PATH) -> tuple[dict, tuple]:
    if not path.exists():
        return {}, ()
    data = json.loads(path.read_text(encoding='utf-8'))
    return data['scores'], tuple(data['combinations'])


STRENGTH_SCORES, COMBINATION_RECOMMENDATIONS = load_strength_scores()


def load_encyclopedia(path: Path = ENCYCLOPEDIA_PATH) -> tuple[dict, dict, dict]:
    if not path.exists():
        return {}, {}, {}
    rows = json.loads(path.read_text(encoding='utf-8'))['entries']
    grouped = {}
    aliases = {}
    for row in rows:
        canonical = row['canonical_name']
        grouped.setdefault(canonical, []).append(row)
        aliases[re.sub(r'[\s·•・&＆:：【】\[\]（）()]', '', row['name'])] = canonical
    # 云外镜有黑、白两行；用样本量较多的一行作评分依据，定位则可合并。
    primary = {
        name: max(items, key=lambda item: (
            (item['wins'] or 0) + (item['losses'] or 0),
            ENCYCLOPEDIA_POINTS.get(item['rating'], -99)))
        for name, items in grouped.items()
    }
    return primary, aliases, grouped


ENCYCLOPEDIA, NAME_ALIASES, ENCYCLOPEDIA_VARIANTS = load_encyclopedia()


def normalize_name(name: str, catalog: Mapping[str, Entry] = CATALOG) -> str | None:
    """接受完整名称或 OCR 前后混入少量属性字的唯一名称。"""
    if '?' in (name or '') or '？' in (name or ''):
        return None
    value = re.sub(r'[\s·•・&＆:：【】\[\]（）()]', '', name or '')
    # 阵容详情的描边字体会把“祢”识别成“称”。限定完整式神名。
    value = value.replace('灶门称豆子', '灶门祢豆子')
    if value in catalog:
        return value
    if value in NAME_ALIASES and NAME_ALIASES[value] in catalog:
        return NAME_ALIASES[value]
    if value in OCR_NAME_ALIASES and OCR_NAME_ALIASES[value] in catalog:
        return OCR_NAME_ALIASES[value]
    value = re.sub(r'^(?:SP|SSR|SR|UR|R|联动)', '', value, flags=re.I)
    if value in catalog:
        return value
    if value in NAME_ALIASES and NAME_ALIASES[value] in catalog:
        return NAME_ALIASES[value]
    fixed = value.translate(_OCR_FIX_TABLE)
    if fixed != value:
        if fixed in catalog:
            return fixed
        if fixed in NAME_ALIASES and NAME_ALIASES[fixed] in catalog:
            return NAME_ALIASES[fixed]
        if value == '升' and '奕' in catalog:
            # 图鉴里这张 R 卡写作“奕”, 游戏显示为“弈”
            return '奕'
    # 最后兜底: 名字够长时允许一个字不同, 必须唯一命中, 防描边字近形误认
    # 两个字的名字也放开(般右 -> 般若), 但同样要求唯一命中
    if len(value) >= 2:
        near = [candidate for candidate in catalog
                if len(candidate) == len(value)
                and sum(left != right for left, right in zip(value, candidate)) == 1]
        if len(near) == 1:
            return near[0]
    # 两字以上误认时用相似度兜底（神塑八岐天蛇 -> 神堕八岐大蛇, 相似度≈0.83）;
    # 阈值 + 首字一致/极高相似度, 避免把没进图鉴的新卡硬套到别的名字上。
    if len(value) >= 3:
        best, best_ratio = None, 0.0
        for candidate in catalog:
            if abs(len(candidate) - len(value)) > 1:
                continue
            ratio = difflib.SequenceMatcher(None, value, candidate).ratio()
            if ratio > best_ratio:
                best, best_ratio = candidate, ratio
        if best is not None and best_ratio >= 0.7 and (
                value[0] == best[0] or best_ratio >= 0.85):
            return best
    matches = [candidate for candidate in catalog if candidate in value]
    if not matches:
        # OCR 漏字：value 是某个完整名的前缀/子串（如“稻荷神御馔”漏“津”）。
        # 仅当唯一候选且 value 长度 >= 2 时才归一化，防止单字误匹配。
        sub_matches = [candidate for candidate in catalog
                       if value in candidate and len(value) >= 2]
        if len(sub_matches) == 1:
            return sub_matches[0]
        return None
    length = max(map(len, matches))
    longest = [candidate for candidate in matches if len(candidate) == length]
    return longest[0] if len(longest) == 1 else None


def role_tags(entry: Entry, *, defense: int | None = None) -> set[str]:
    if entry.name in USER_ROLES:
        return set(USER_ROLES[entry.name])
    if entry.name == '云外镜':
        # 白镜侧重驱散与拉条；防御面板不明时按黑镜保守估值。
        return {'support', 'tempo'} if defense is not None and defense >= 900 else {'damage'}
    role = entry.role
    tags = set()
    if '火机' in role or '鬼火' in role:
        tags.add('fire')
    if any(word in role for word in ('奶', '治疗', '复活')):
        tags.add('sustain')
    if any(word in role for word in ('盾', '减伤', '保单核', '抗单体')):
        tags.add('shield')
    if any(word in role for word in ('控制', '推条')):
        tags.add('control')
    if '输出' in role or '增伤' in role:
        tags.add('damage' if '输出' in role else 'support')
    if '拉条' in role:
        tags.add('tempo')
    if '辅助' in role or '驱散' in role or '解控' in role:
        tags.add('support')
    extra = ENCYCLOPEDIA.get(entry.name, {}).get('role', '')
    if extra == '打火':
        tags.add('fire')
    elif extra == '治疗':
        tags.add('sustain')
    elif extra == '护卫':
        tags.add('shield')
    elif extra == '控制':
        tags.add('control')
    elif extra == '输出':
        tags.add('damage')
    elif extra == '拉条':
        tags.add('tempo')
    elif extra == '综合':
        tags.add('support')
    return tags


def is_fire_supplier(name: str, catalog: Mapping[str, Entry] = CATALOG) -> bool:
    entry = catalog.get(name)
    return entry is not None and 'fire' in role_tags(entry)


def encyclopedia_points(name: str, *, team: Iterable[str] = (),
                        defense: int | None = None) -> float:
    """百科评级和胜率是辅助信号；小样本胜率向 50% 收缩。"""
    row = ENCYCLOPEDIA.get(name)
    if name == '云外镜':
        variant = '云外镜（白）' if defense is not None and defense >= 900 else '云外镜（黑）'
        row = next((item for item in ENCYCLOPEDIA_VARIANTS.get(name, ())
                    if item['name'] == variant), row)
    if not row:
        return 0.0
    samples = (row['wins'] or 0) + (row['losses'] or 0)
    rating = row['rating']
    if name in USER_RATINGS:
        rating = USER_RATINGS[name]
    if name == '茨木童子':
        rating = 'S' if '云间不见岳' in team else 'A'
    grade = ENCYCLOPEDIA_POINTS.get(rating, 0)
    user_rating = name in USER_RATINGS or name == '茨木童子'
    grade_weight = 1.0 if user_rating else min(1.0, max(0.5, samples / 300)) if samples else 0.5
    shrunk_winrate = ((row['wins'] or 0) + 50) / (samples + 100)
    # 百科胜率是唯一的大样本实战信号（单卡 100-1000 场），权重需能压过稀有度直觉。
    winrate_points = max(-14, min(14, (shrunk_winrate - 0.5) * 56)) if samples else 0
    return grade * grade_weight + winrate_points


def tier_points(entry: Entry, team: Iterable[str] = (), *, speed: int | None = None,
                defense: int | None = None) -> int:
    """条件未满足时取保守等级；未知等级不凭稀有度加分。"""
    if entry.name in USER_TIERS:
        return TIER_POINTS[USER_TIERS[entry.name]]
    if entry.name == '梦山白藏主':
        return 40
    label = entry.tier
    if label in TIER_POINTS:
        return TIER_POINTS[label]
    if label in ('?', '？', ''):
        return 0
    names = set(team)
    if entry.name == '云外镜':
        return 40 if defense is not None and defense >= 900 else 20
    if entry.name == '茨木童子':
        return 40 if '云间不见岳' in names else 30
    if '220以上特等' in label and speed is not None:
        return 40 if speed >= 220 else 30 if speed >= 210 else 20
    if '220以上上等' in label and speed is not None:
        return 30 if speed >= 220 else 20 if speed >= 210 else 10
    if '170上等' in label and speed is not None:
        return 30 if speed >= 170 else 20
    if '火山队特等' in label or '火系特等' in label:
        return 40 if flame_partners(names) else 30 if '上等' in label else 10
    if '有季特等' in label:
        return 40 if '季' in names else 30
    if 'SP千姬上等' in label:
        return 30 if '鲸汐千姬' in names else 20
    if '伤魂鸟上等' in label:
        return 30 if '伤魂鸟' in names else 20
    if '破势特等' in label:
        return 40 if '破势' in names else 30
    if '上等到特等之间' in label or '上等特等之间' in label:
        return 35
    if '单上中等' in label or '120速中等' in label:
        return 20
    if '中等' in label:
        return 20
    if '上等' in label:
        return 30
    if '特等' in label:
        return 40
    if '下等' in label:
        return 10
    return 0


def strength_points(entry: Entry, team: Iterable[str] = (), *, speed: int | None = None,
                    defense: int | None = None) -> float:
    """新表独立强度分；用户指定评级及条件形态保持优先。"""
    old = tier_points(entry, team, speed=speed, defense=defense)
    if entry.name in USER_TIERS or entry.name in USER_RATINGS or entry.name in (
            '云外镜', '茨木童子'):
        return float(old)
    score = STRENGTH_SCORES.get(entry.name, {}).get('strength')
    if not isinstance(score, (int, float)):
        return float(old)
    if entry.tier not in TIER_POINTS and entry.tier not in ('', '?', '？'):
        return float(old)
    points = ((60, 10), (74, 20), (85, 30), (94, 40))
    if score <= 60:
        return max(0.0, score / 6)
    for (low, low_points), (high, high_points) in zip(points, points[1:]):
        if score <= high:
            return low_points + (score - low) * (high_points - low_points) / (high - low)
    return min(45.0, 40.0 + (score - 94))


# 从“组合推荐”中提取可直接由式神名称判断的核心搭配。泛指“输出/火机”
# 由 role_tags 与队伍平衡评分处理，避免把说明文本当作可点选的式神。
COMBINATION_PAIRS = (
    ({'云间不见岳', '炼狱茨木童子'}, 9),
    ({'云间不见岳', '烬天玉藻前'}, 9),
    ({'鬼王酒吞童子', '禅心云外镜'}, 10),
    ({'浮世青行灯', '心狩鬼女红叶'}, 9),
    ({'食灵', '姑获鸟'}, 8),
    ({'食灵', '鬼童丸'}, 7),
    ({'食灵', '心友犬神'}, 7),
    ({'空相面灵气', '大夜摩天阎魔'}, 8),
    ({'龙珏', '歌留多'}, 10),
    ({'不知火', '酒吞童子'}, 8),
    ({'不知火', '吸血姬'}, 8),
    ({'鲸汐千姬', '初音未来'}, 9),
    ({'季', '犬神'}, 8),
    ({'一目连', '匣中少女'}, 7),
    ({'鲤鱼精', '匣中少女'}, 7),
    ({'蚀月吸血姬', '阎魔'}, 7),
    ({'神启荒', '大夜摩天阎魔'}, 7),
    ({'神启荒', '遥念烟烟罗'}, 7),
    ({'言灵', '鬼金羊'}, 9),
    ({'入内雀', '荒骷髅'}, 7),
)


def suggested_souls(entry: Entry) -> tuple[str, ...]:
    found = [(entry.souls.index(soul), soul) for soul in SOUL_NAMES
             if soul in entry.souls]
    result = [soul for _, soul in sorted(found)[:3]]
    extra = ENCYCLOPEDIA.get(entry.name)
    if extra and len(result) < 3:
        text = str(extra['summary']) + '\n' + str(extra['panel'])
        for soul in SOUL_NAMES:
            if soul in text and soul not in result:
                result.append(soul)
                if len(result) == 3:
                    break
    return tuple(result)


def _winrate_ok(name: str, floor: float = 0.5) -> bool:
    """弱输出(百科胜率明显低于 50%)不算进"2 输出"公式, 免得为凑数上 34% 胜率的输出。"""
    row = ENCYCLOPEDIA.get(name)
    if not row:
        return True
    samples = (row['wins'] or 0) + (row['losses'] or 0)
    if samples < 50:
        return True
    return ((row['wins'] or 0) + 50) / (samples + 100) >= floor


def _strong_support(name: str) -> bool:
    """强力增伤/辅助: 大样本胜率过半(如食灵 52.3%/1000 场)或者特等强度卡, 可以顶掉治疗/盾位或控制/拉条位。"""
    if name in USER_STRONG_SUPPORT:
        return True
    row = ENCYCLOPEDIA.get(name) or {}
    samples = (row.get('wins') or 0) + (row.get('losses') or 0)
    if samples >= 200:
        return (row.get('winrate') or 0) >= 0.52
    entry = CATALOG.get(name)
    if entry is None:
        return False
    strength = STRENGTH_SCORES.get(name, {}).get('strength')
    return entry.tier == '特等' and isinstance(strength, (int, float)) and strength >= 35


def _is_amplifier(name: str, catalog: Mapping[str, Entry] = CATALOG) -> bool:
    """标记为增伤的辅助(区别于驱散/解控这类纯辅助), 强增伤可以顶掉辅助位。"""
    entry = catalog.get(name)
    if entry is None:
        return False
    text = f"{entry.role}{ENCYCLOPEDIA.get(name, {}).get('role', '')}{entry.notes[:60]}"
    return '增伤' in text


def composition_counts(names: Iterable[str], catalog: Mapping[str, Entry] = CATALOG,
                       defenses: Mapping[str, int] | None = None) -> dict[str, int]:
    """按用户公式统计编排位：2 打火 + 2 输出 + 1 治疗/盾 + 1 控制或拉条。

    输出按角色标签计, 不看胜率(胜率低的输出只降评分, 不能让公式永远凑不齐);
    强力增伤算"万能辅助位", 同时顶治疗/盾位和控制/拉条位(protect/flex 各 +1)。
    """
    names = list(names)
    defens = defenses or {}
    tags = [role_tags(catalog[name], defense=defens.get(name)) for name in names]
    fire = sum('fire' in tag for tag in tags)
    damage = sum('damage' in tag for tag in tags)
    weak_damage = sum('damage' in tag and not _winrate_ok(name, 0.45)
                      for tag, name in zip(tags, names))
    protect = sum(bool({'sustain', 'shield'} & tag) for tag in tags)
    flex = sum(bool({'control', 'tempo'} & tag) for tag in tags)
    amplify = sum('support' in tag and 'damage' not in tag
                  and _is_amplifier(name, catalog) and _strong_support(name)
                  for tag, name in zip(tags, names))
    # 纯奶: 只有治疗/盾, 不带控制/拉条/增伤(用户口径: 两个辅助位尽量别全是奶)
    heal_only = sum(bool({'sustain', 'shield'} & tag)
                    and not ({'control', 'tempo', 'support', 'damage'} & tag)
                    for tag in tags)
    return {'fire': fire, 'damage': damage, 'weak_damage': weak_damage,
            'protect': protect + amplify, 'flex': flex + amplify,
            'amplify': amplify, 'heal_only': heal_only}


def team_score(names: Iterable[str], catalog: Mapping[str, Entry] = CATALOG,
               speeds: Mapping[str, int] | None = None,
               defenses: Mapping[str, int] | None = None) -> float:
    """统一矩阵评分；需要分项时使用 matrix.score_team。"""
    from tasks.DraftDuel.matrix import score_team
    return score_team(names, catalog, speeds, defenses).total


def _strong_card(name: str) -> bool:
    """百科大样本高胜率的卡：即使角色标签对不上公式也不应被硬过滤掉。"""
    row = ENCYCLOPEDIA.get(name)
    if not row:
        return False
    samples = (row['wins'] or 0) + (row['losses'] or 0)
    return samples >= 200 and (row['winrate'] or 0) >= 0.56


def _premium_dps(name: str, catalog: Mapping[str, Entry] = CATALOG) -> bool:
    """强力输出：用户钦定的输出核心, 或百科大样本胜率过半, 或特等且强度分够高。"""
    entry = catalog.get(name)
    if entry is None or 'damage' not in role_tags(entry):
        return False
    if name in USER_STRONG_DPS:
        return True
    row = ENCYCLOPEDIA.get(name) or {}
    samples = (row.get('wins') or 0) + (row.get('losses') or 0)
    if samples >= 150 and (row.get('winrate') or 0) >= 0.50:
        return True
    strength = STRENGTH_SCORES.get(name, {}).get('strength')
    return entry.tier == '特等' and isinstance(strength, (int, float)) and strength >= 35


def rank_picks(offers: Iterable[str], team: Iterable[str] = (), *,
               mate: Iterable[str] = (), team_size: int = 5,
               speeds: Mapping[str, int] | None = None,
               defenses: Mapping[str, int] | None = None,
               catalog: Mapping[str, Entry] = CATALOG) -> tuple[Pick, ...]:
    """返回全部可识别候选的矩阵分项及预期上场阵容，按总分降序。"""
    from tasks.DraftDuel.matrix import rank_picks as matrix_rank_picks
    return matrix_rank_picks(offers, team, mate=mate, team_size=team_size,
                             speeds=speeds, defenses=defenses, catalog=catalog)


def choose_pick(offers: Iterable[str], team: Iterable[str] = (), *,
                mate: Iterable[str] = (), team_size: int = 5,
                speeds: Mapping[str, int] | None = None,
                defenses: Mapping[str, int] | None = None,
                catalog: Mapping[str, Entry] = CATALOG) -> Pick:
    """选进五张备选池：比较最终上场价值、备选价值及轮次倾向。"""
    return rank_picks(offers, team, mate=mate, team_size=team_size,
                      speeds=speeds, defenses=defenses, catalog=catalog)[0]


def plan_lineup(available: Iterable[str], *, team_size: int = 5,
                locked: Iterable[str] = (), speeds: Mapping[str, int] | None = None,
                defenses: Mapping[str, int] | None = None,
                max_fire: int | None = None,
                catalog: Mapping[str, Entry] = CATALOG) -> tuple[str, ...]:
    """给定完整候选池时搜索高分阵容；locked 中的成员必须保留。"""
    required = tuple(locked)
    if not 1 <= team_size <= 8 or len(required) > team_size:
        raise ValueError('队伍人数配置无效')
    if len(required) != len(set(required)) or any(name not in catalog for name in required):
        raise ValueError('已选阵容含重复或未知式神')
    if len(SUMMON_SLOT_USERS.intersection(required)) > 1:
        raise ValueError('已选阵容占用多个我方召唤位')
    if max_fire is not None and (max_fire < 0 or
            sum(is_fire_supplier(name, catalog) for name in required) > max_fire):
        raise ValueError('已上阵打火式神数量超过限制')
    candidates = sorted({name for raw in available
                         if (name := normalize_name(raw, catalog)) and name not in required})
    if len(candidates) + len(required) < team_size:
        raise ValueError('可识别候选不足以组成队伍')
    # 保留多种中途组合，让火机、盾和输出的搭配可以在后几手形成。
    from tasks.DraftDuel.matrix import ScoringMatrix
    matrix = ScoringMatrix((*required, *candidates), catalog, speeds, defenses)
    field_table = duel_stats._tally('fielded') if duel_stats is not None else None
    states = [required]
    for _ in range(team_size - len(required)):
        next_states = {}
        for state in states:
            for name in candidates:
                if name not in state:
                    lineup = (*state, name)
                    if len(SUMMON_SLOT_USERS.intersection(lineup)) > 1:
                        continue
                    if max_fire is not None and sum(
                            is_fire_supplier(item, catalog) for item in lineup) > max_fire:
                        continue
                    key = tuple(sorted(lineup))
                    next_states[key] = lineup
        if not next_states:
            raise ValueError('召唤位或打火数量限制后候选不足以组成阵容')
        states = sorted(next_states.values(),
                        key=lambda lineup: (matrix.score(lineup, expected_size=team_size).total +
                                            (sum(duel_stats.field_bonus(item, field_table)
                                                 for item in lineup) if duel_stats else 0.0),
                                            lineup),
                        reverse=True)[:128]
    return states[0]


def plan_duel_lineup(own: Iterable[str], mate: Iterable[str], *, locked: Iterable[str] = (),
                     team_size: int = 3,
                     catalog: Mapping[str, Entry] = CATALOG) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """穷举双方三加三，按统一矩阵总分选择；公式缺口为软评分。"""
    from tasks.DraftDuel.matrix import best_plan
    own_pool = {name for raw in own if (name := normalize_name(raw, catalog))}
    mate_pool = {name for raw in mate if (name := normalize_name(raw, catalog))}
    if not 1 <= team_size <= 8 or min(len(own_pool), len(mate_pool)) < team_size:
        raise ValueError('协同对弈任一账号备选不足或人数无效')
    field_table = duel_stats._tally('fielded') if duel_stats is not None else None
    plan = best_plan(own_pool, mate_pool, own_size=team_size, mate_size=team_size,
                     locked=locked, catalog=catalog, field_table=field_table)
    return plan.own, plan.mate
