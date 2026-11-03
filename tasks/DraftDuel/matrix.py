"""协同对弈能力矩阵、稀疏搭配矩阵及统一阵容评分（不依赖游戏画面）。

单卡资料及用户指定搭配仍由 strategy 提供。能力行是实际功能，双卡矩阵
每对只计一次，三人组合单独计分；未知搭配为零，不推测克制关系。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

from tasks.DraftDuel import strategy as data


# 分值沿用原策略的量级；集中在这里，便于按候选分项日志调参。
WEIGHTS = {
    'encyclopedia': 3.0, 'versatility': 0.18,
    'fire': 18.0, 'damage': 10.0, 'two_damage': 14.0,
    'protect': 10.0, 'flex': 8.0, 'balanced_support': 6.0,
    'missing_fire': 14.0, 'no_fire': 26.0, 'missing_damage': 22.0,
    'missing_protect': 14.0, 'missing_flex': 12.0,
    'secondary_fire': 0.65, 'fire_quality': 10.0,
    'extra_fire': 16.0, 'extra_damage': 20.0,
    'extra_protect': 15.0, 'extra_healer': 10.0,
    'grass_protect': 18.0, 'weak_damage': 6.0,
    'no_fire_core': 9.0, 'missing_partner': 24.0,
    'reserve': 0.12, 'early_damage': 24.0,
    'draft_fire': 8.0, 'priority_fire': 5.0,
    'flame_partner': 12.0,
}

ROLE_LABELS = {'fire': '供火', 'damage': '输出', 'sustain': '治疗',
               'shield': '护盾', 'control': '控制', 'tempo': '行动条',
               'support': '辅助'}

BASE_PAIRS = (
    ({'初音未来', '鲸汐千姬'}, 9),
    ({'桃花妖', '季'}, 8), ({'桃花妖', '云外镜'}, 8),
    ({'桃花妖', '镜音铃连'}, 8), ({'稻荷神御馔津', '纺愿缘结神'}, 8),
    ({'犬神', '季'}, 8), ({'夏目猫老师', '猫川'}, 14),
    ({'帝释天', '纺愿缘结神'}, -8),
)


@dataclass(frozen=True)
class Ability:
    name: str
    tags: frozenset[str]
    fire_units: float
    damage: bool
    protect: bool
    flex: bool
    amplify: bool
    heal_only: bool


@dataclass(frozen=True)
class Score:
    parts: tuple[tuple[str, float], ...]
    details: tuple[str, ...] = ()

    @property
    def total(self) -> float:
        return sum(value for _, value in self.parts)

    def summary(self) -> str:
        return ' '.join(f'{label}{value:+.1f}' for label, value in self.parts)


@dataclass(frozen=True)
class Plan:
    own: tuple[str, ...]
    mate: tuple[str, ...]
    score: Score


def legal_lineup(names: Iterable[str]) -> bool:
    names = tuple(names)
    return (len(names) == len(set(names))
            and len(data.SUMMON_SLOT_USERS.intersection(names)) <= 1)


class ScoringMatrix:
    """一次决策内复用能力行和对称的稀疏双卡矩阵。"""

    def __init__(self, names: Iterable[str], catalog=None, speeds=None, defenses=None):
        self.catalog = data.CATALOG if catalog is None else catalog
        self.speeds, self.defenses = speeds or {}, defenses or {}
        self.abilities = {}
        for name in sorted(set(names)):
            if name not in self.catalog:
                raise ValueError(f'未知式神: {name}')
            entry = self.catalog[name]
            tags = frozenset(data.role_tags(entry, defense=self.defenses.get(name)))
            fire = (WEIGHTS['secondary_fire'] if name in data.SECONDARY_FIRE
                    else 1.0) if 'fire' in tags else 0.0
            self.abilities[name] = Ability(
                name, tags, fire, 'damage' in tags,
                bool({'sustain', 'shield'} & tags),
                bool({'control', 'tempo'} & tags),
                ('support' in tags and 'damage' not in tags
                 and data._is_amplifier(name, self.catalog) and data._strong_support(name)),
                bool({'sustain', 'shield'} & tags)
                and not bool({'control', 'tempo', 'support', 'damage'} & tags),
            )
        # 后面的用户规则覆盖资料规则，防止同一对组合重复相加。
        rules = {}
        for members, points in (*BASE_PAIRS, *data.COMBINATION_PAIRS, *data.USER_COMBOS):
            members = frozenset(members)
            if members <= self.abilities.keys():
                rules[members] = float(points)
        self.pairs = {members: points for members, points in rules.items() if len(members) == 2}
        self.combos = {members: points for members, points in rules.items() if len(members) > 2}

    def score(self, names: Iterable[str], *, expected_size: int = 6) -> Score:
        if expected_size < 1:
            raise ValueError('预期阵容人数必须大于零')
        # 固定求和顺序，避免双方输入顺序不同引起浮点尾差影响同分决策。
        team = tuple(sorted(names))
        if len(team) != len(set(team)):
            raise ValueError('阵容中不能重复选择同名式神')
        rows = [self.abilities[name] for name in team]
        if not rows:
            return Score(())
        members = set(team)
        progress = min(1.0, len(team) / expected_size)
        fire_target = 2 if expected_size >= 4 else 1
        damage_target = 2
        fire = sum(row.fire_units > 0 for row in rows)
        damage = sum(row.damage for row in rows)
        protect = sum(row.protect for row in rows)
        flex = sum(row.flex for row in rows)
        # 同一张纯增伤辅助只能替一个缺口，不能同时充当两个独立位置。
        cover_protect, cover_flex = protect, flex
        for row in rows:
            if row.amplify and not row.protect and not row.flex:
                if not cover_protect:
                    cover_protect += 1
                elif not cover_flex:
                    cover_flex += 1

        strength = sum(data.strength_points(self.catalog[name], team,
                       speed=self.speeds.get(name), defense=self.defenses.get(name))
                       for name in team)
        evidence = WEIGHTS['encyclopedia'] * sum(data.encyclopedia_points(
            name, team=team, defense=self.defenses.get(name)) for name in team)
        versatility = sum((source['combo'] - 75) * WEIGHTS['versatility']
                          for name in team
                          if isinstance((source := data.STRENGTH_SCORES.get(name, {}))
                                        .get('combo'), (int, float)))
        roles = set().union(*(row.tags for row in rows))
        coverage = sum(points for role, points in data.ROLE_POINTS.items() if role in roles)
        coverage += 4 if damage >= 2 else 0
        coverage += WEIGHTS['fire'] * min(fire, fire_target)
        coverage += WEIGHTS['damage'] * min(damage, damage_target)
        coverage += WEIGHTS['two_damage'] if damage >= 2 else 0
        coverage += WEIGHTS['protect'] if cover_protect else 0
        coverage += WEIGHTS['flex'] if cover_flex else 0
        coverage += WEIGHTS['balanced_support'] if cover_protect and cover_flex else 0

        deficit = WEIGHTS['missing_fire'] * max(0, fire_target - fire)
        deficit += WEIGHTS['no_fire'] if fire == 0 and len(team) >= 2 else 0
        deficit += WEIGHTS['missing_damage'] if damage == 0 and len(team) >= 2 else 0
        deficit += WEIGHTS['missing_protect'] if not cover_protect else 0
        deficit += WEIGHTS['missing_flex'] if not cover_flex else 0
        deficit += WEIGHTS['fire_quality'] * max(
            0, min(fire, fire_target) - sum(row.fire_units for row in rows))
        deficit *= progress

        redundancy = WEIGHTS['extra_fire'] * max(0, fire - fire_target)
        redundancy += WEIGHTS['extra_damage'] * max(0, damage - damage_target)
        redundancy += WEIGHTS['extra_protect'] * max(0, protect - 1)
        redundancy += WEIGHTS['extra_healer'] * max(0, sum(row.heal_only for row in rows) - 1)
        redundancy += 20 * max(0, len(data.SUMMON_SLOT_USERS & members) - 1)
        if {'椒图', '莹草'} <= members:
            redundancy += WEIGHTS['grass_protect'] * max(0, protect - 2)
        redundancy += WEIGHTS['weak_damage'] * sum(
            row.damage and not data._winrate_ok(row.name, 0.45) for row in rows)

        hits = [(combo, points) for combo, points in (*self.pairs.items(), *self.combos.items())
                if combo <= members]
        synergy = sum(points for _, points in hits)
        details = [f'{"＋".join(sorted(combo))} {points:+.1f}' for combo, points in hits]
        conditions = 0.0
        flame = data.flame_partners(members)
        if flame:
            # 核心只触发一次；两个核心同时在场不重复叠加。
            conditions += WEIGHTS['flame_partner'] * min(len(flame), 4)
            details.append('火系体系：核心=' + '＋'.join(sorted(members & data.FLAME_ENABLERS))
                           + '；成员=' + '＋'.join(sorted(flame)))
        low_fire = len(data.NO_FIRE_CORE & members)
        if fire == 0 and low_fire >= 2:
            conditions += WEIGHTS['no_fire_core'] * min(low_fire, 4)
            details.append('无火核心体系')
        if '初音未来' in members and '鲸汐千姬' not in members:
            conditions -= WEIGHTS['missing_partner']
            details.append('初音未来缺少鲸汐千姬')
        overlap = max((len(lineup & members) for lineup in data.SIX_WIN_LINEUPS), default=0)
        precedent = data.SIX_WIN_OVERLAP_POINTS.get(overlap, 0)
        precedent += sum(1.5 * min(2, data.SIX_WIN_FREQUENCY[name] - 1)
                         for name in team if data.SIX_WIN_FREQUENCY.get(name, 0) > 1)
        return Score((('单卡', strength), ('资料', evidence), ('泛用', versatility),
                      ('补位', coverage), ('缺口', -deficit), ('重复', -redundancy),
                      ('搭配', synergy), ('条件', conditions), ('先例', precedent)),
                     tuple(details))


def score_team(names, catalog=None, speeds=None, defenses=None, *, expected_size=6,
               include_stats=False) -> Score:
    names = tuple(names)
    score = ScoringMatrix(names, catalog, speeds, defenses).score(names, expected_size=expected_size)
    if include_stats and data.duel_stats is not None:
        table = data.duel_stats._tally('fielded')
        bonus = sum(data.duel_stats.field_bonus(n, table) for n in sorted(names))
        score = Score((*score.parts, ('实战', bonus)), score.details)
    return score


def best_plan(own, mate=(), *, own_size=3, mate_size=3, locked=(),
              catalog=None, speeds=None, defenses=None, max_fire=None,
              matrix=None, field_table=None, expected_size=None) -> Plan:
    """穷举合法组合；相同快照下，交换双方输入会得到互换的规划结果。"""
    catalog = data.CATALOG if catalog is None else catalog
    own, mate = tuple(sorted(set(own))), tuple(sorted(set(mate)))
    required = tuple(locked)
    if (own_size < 0 or mate_size < 0 or own_size > len(own) or mate_size > len(mate)
            or len(required) != len(set(required)) or len(required) > own_size
            or not set(required) <= set(own)):
        raise ValueError('上阵人数或已锁定式神无效')
    matrix = matrix or ScoringMatrix((*own, *mate), catalog, speeds, defenses)
    best, best_rank = None, None
    expected = expected_size if expected_size is not None else (6 if mate else 3)
    for own_pick in combinations(own, own_size):
        if not set(required) <= set(own_pick):
            continue
        for mate_pick in combinations(mate, mate_size if mate else 0):
            team = own_pick + mate_pick
            if not legal_lineup(team):
                continue
            if max_fire is not None and sum(matrix.abilities[n].fire_units > 0
                                            for n in own_pick) > max_fire:
                continue
            score = matrix.score(team, expected_size=expected)
            if field_table is not None and data.duel_stats is not None:
                # 同时计算两侧，避免每个实例只奖励自己的三张而得出冲突计划。
                bonus = sum(data.duel_stats.field_bonus(n, field_table) for n in sorted(team))
                score = Score((*score.parts, ('实战', bonus)), score.details)
            canonical = (own_pick, mate_pick) if own <= mate else (mate_pick, own_pick)
            rank = (score.total, canonical)
            if best_rank is None or rank > best_rank:
                best, best_rank = Plan(own_pick, mate_pick, score), rank
    if best is None:
        raise ValueError('没有满足人数、重名或召唤位限制的上阵组合')
    return best


def joint_draft_choices(offers, team, mate_offers, mate, *, catalog=None):
    """同轮双方候选联合比较；只返回预期选择，不将其写成已选卡。"""
    catalog = data.CATALOG if catalog is None else catalog
    own_rank = {p.name: p for p in rank_picks(offers, team, mate=mate, catalog=catalog)}
    peer_rank = {p.name: p for p in rank_picks(mate_offers, mate, mate=team, catalog=catalog)}
    matrix = ScoringMatrix((*team, *mate, *own_rank, *peer_rank), catalog)
    best = None
    captain_order = (tuple(sorted(team)), tuple(sorted(own_rank))) <= (tuple(sorted(mate)), tuple(sorted(peer_rank)))
    for own in sorted(own_rank):
        for peer in sorted(peer_rank):
            if own == peer or own in mate or peer in team:
                continue
            pools = ((*team, own), (*mate, peer))
            limits = tuple(min(3, len(pool)) for pool in pools)
            plans = []
            for count in range(sum(limits), -1, -1):
                for own_size in range(limits[0] + 1):
                    peer_size = count - own_size
                    if not 0 <= peer_size <= limits[1]:
                        continue
                    try:
                        plans.append(best_plan(*pools, own_size=own_size, mate_size=peer_size,
                                               catalog=catalog, matrix=matrix, expected_size=6))
                    except ValueError:
                        continue
                if plans:
                    break
            if not plans:
                continue
            plan = max(plans, key=lambda p: p.score.total)
            reserve = WEIGHTS['reserve'] * sum(max(0, data.strength_points(catalog[n]))
                       for n in sorted(set(pools[0] + pools[1]) - set(plan.own + plan.mate)))
            tendency = sum(value for row in (own_rank[own], peer_rank[peer])
                           for label, value in row.score_parts if label == '选卡倾向')
            canonical = (own, peer) if captain_order else (peer, own)
            key = (len(plan.own) + len(plan.mate), plan.score.total + reserve + tendency, canonical)
            if best is None or key > best[0]:
                best = (key, own, peer)
    if best is None:
        raise ValueError('双方候选没有合法组合')
    return best[1], best[2]


def rank_picks(offers, team=(), *, mate=(), team_size=5, speeds=None,
               defenses=None, catalog=None) -> tuple[data.Pick, ...]:
    catalog = data.CATALOG if catalog is None else catalog
    current = tuple(team)
    if not 1 <= team_size <= 8 or len(current) >= team_size:
        raise ValueError('队伍人数配置无效或已满')
    if len(current) != len(set(current)) or any(n not in catalog for n in current):
        raise ValueError('已选式神含重名或未知名字')
    mate = tuple(sorted({name for raw in mate if (name := data.normalize_name(raw, catalog))}))
    candidates = sorted({name for raw in offers
                         if (name := data.normalize_name(raw, catalog)) and name not in current})
    if not candidates:
        raise ValueError('没有可识别且未入队的候选式神')
    matrix = ScoringMatrix((*current, *mate, *candidates), catalog, speeds, defenses)

    def pool_plan(pool):
        # 备选可以有重名/召唤位冲突。先争取最多可用位置，冲突卡留在备选，
        # 不因前两手拿了两张召唤位卡就让后续选卡无法评分。
        own_limit, mate_limit = min(3, len(pool)), min(3, len(mate))
        for count in range(own_limit + mate_limit, -1, -1):
            plans = []
            for own_count in range(own_limit, -1, -1):
                mate_count = count - own_count
                if not 0 <= mate_count <= mate_limit:
                    continue
                try:
                    plans.append(best_plan(pool, mate, own_size=own_count,
                                 mate_size=mate_count, catalog=catalog, matrix=matrix))
                except ValueError:
                    continue
            if plans:
                return max(plans, key=lambda p: (p.score.total, p.own, p.mate))
        raise ValueError('没有可评估的备选阵容')

    baseline = pool_plan(current)
    pick_table = data.duel_stats._tally('ours') if data.duel_stats is not None else None

    def reserve_value(pool, plan):
        return WEIGHTS['reserve'] * sum(max(0.0, data.strength_points(catalog[n])
               + WEIGHTS['encyclopedia'] * data.encyclopedia_points(n))
               for n in sorted(set(pool) - set(plan.own)))

    ranked = []
    for name in candidates:
        plan = pool_plan((*current, name))
        before = dict(baseline.score.parts)
        parts = [(label, value - before.get(label, 0.0)) for label, value in plan.score.parts]
        if not parts:
            parts = [('阵容提升', 0.0)]
        reserve = reserve_value((*current, name), plan) - reserve_value(current, baseline)
        parts.append(('备选', reserve))
        phase = 0.0
        own_damage = sum(matrix.abilities[n].damage for n in baseline.own)
        if len(current) < 2 and own_damage < 2 and data._premium_dps(name, catalog):
            phase += WEIGHTS['early_damage'] * (1 - len(current) / 2)
        field = baseline.own + baseline.mate
        fire = sum(matrix.abilities[n].fire_units > 0 for n in field)
        fire_target = 2 if mate else 1
        if fire < fire_target and name in data.CORE_FIRE:
            phase += WEIGHTS['draft_fire']
            if name in data.PRIORITY_FIRE:
                phase += WEIGHTS['priority_fire']
        for role in sorted(matrix.abilities[name].tags):
            preferred = data.ROLE_RECOMMEND.get(role, ())
            if name in preferred:
                phase += (data.RECOMMEND_BONUS if preferred.index(name) == 0
                          else data.RECOMMEND_BONUS_SECOND)
                break
        parts.append(('选卡倾向', phase))
        stats = data.duel_stats.pick_bonus(name, pick_table) if data.duel_stats else 0.0
        parts.append(('实战', stats))
        score = Score(tuple(parts), plan.score.details)
        tags = '、'.join(ROLE_LABELS[t] for t in sorted(matrix.abilities[name].tags))
        reasons = (f'能力：{tags}', *score.details)
        ranked.append(data.Pick(name, score.total, reasons, data.suggested_souls(catalog[name]),
                                score.parts, plan.own + plan.mate))
    return tuple(sorted(ranked, key=lambda pick: (pick.score, data.tier_points(
        catalog[pick.name], current, speed=(speeds or {}).get(pick.name),
        defense=(defenses or {}).get(pick.name)), pick.name), reverse=True))
