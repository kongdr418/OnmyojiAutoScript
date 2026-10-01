# This Python file uses the following encoding: utf-8
"""FrogBoss frog_oas 策略的回归自检。

用法: python dev_tools/frog_oas_check.py

覆盖 2026-10-01 13:45 的真实故障：上一轮全体押错后，所有源的权重同时归零，
加权投票给不出任何方向，于是直接抛硬币，压了与博主、大众都相反的一边。
"""
import json
import sys
import tempfile
from pathlib import Path

# 允许直接 python dev_tools/frog_oas_check.py 运行（脚本目录不会自动进 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module.logger import logger  # noqa: E402
from tasks.FrogBoss.frog_oas import OasHistory  # noqa: E402

# 历史记录用的签名，与当前对局签名差异远大于 same_lineup 的 20 位阈值，
# 避免被当成同一阵容而复用旧决策。
SIG_HISTORY = '01' * 256
SIG_CURRENT = '10' * 256

failures = []
scratch = Path(tempfile.mkdtemp(prefix='frog_oas_check_'))


def build(events):
    """把事件写成 jsonl 再加载，顺带覆盖 OasHistory 的读取路径。"""
    path = scratch / f'history_{len(list(scratch.iterdir()))}.jsonl'
    with path.open('w', encoding='utf-8') as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    return OasHistory(path)


def seed(votes, winner=None):
    """构造一条历史决策；给了 winner 就一并结算，用来制造指定权重。"""
    events = [dict(kind='decision', id='seed', slot='seed', signature=SIG_HISTORY,
                   votes=votes, left=0, right=0)]
    if winner:
        events.append(dict(kind='result', id='seed', winner=winner,
                           outcomes={key: value == winner for key, value in votes.items()}))
    return events


def report(name, ok, detail=''):
    if ok:
        logger.info(f'frog_oas check PASS: {name}')
    else:
        logger.error(f'frog_oas check FAIL: {name} | {detail}')
        failures.append(name)


# 案例 1：上一轮全体（含 crowd）押左且全错 → 所有源权重归零。
# 本轮博主 4:2 指左、大众 10315:2565 指左，必须跟左，不能抛硬币压右。
history = build(seed({'A': 'LEFT', 'B': 'LEFT', 'C': 'LEFT', 'D': 'LEFT',
                      'E': 'LEFT', 'F': 'LEFT', 'crowd': 'LEFT'}, winner='RIGHT'))
result = history.choose(SIG_CURRENT, 10315, 2565, [
    {'uid': uid, 'side': side} for uid, side in
    [('A', 'LEFT'), ('B', 'LEFT'), ('C', 'LEFT'), ('D', 'LEFT'),
     ('E', 'RIGHT'), ('F', 'RIGHT')]
])
report('上轮全错+博主与大众同指左时不得压右',
       result['side'] == 'LEFT' and result['scores']['LEFT'] > result['scores']['RIGHT'],
       f"side={result['side']} scores={result['scores']} weights={result['weights']} "
       f"basis={result['basis']}")

# 案例 2：冷启动行为不得改变 —— 博主 6:3 明确指左，跟博主。
history = build([])
result = history.choose(SIG_CURRENT, 100, 50, [
    {'uid': uid, 'side': side} for uid, side in
    [('A', 'LEFT'), ('B', 'LEFT'), ('C', 'LEFT'), ('D', 'LEFT'), ('E', 'LEFT'),
     ('F', 'LEFT'), ('G', 'RIGHT'), ('H', 'RIGHT'), ('I', 'RIGHT')]
])
report('冷启动跟随博主多数',
       result['side'] == 'LEFT' and result['basis'] == 'expert'
       and result['scores'] == {'LEFT': 6.0, 'RIGHT': 3.0},
       f"side={result['side']} basis={result['basis']} scores={result['scores']}")

# 案例 3：冷启动且博主只有 1:0（差值不超过 EXPERT_CLEAR_MARGIN）→ 视为难分秋色，随大众。
history = build([])
result = history.choose(SIG_CURRENT, 10, 90, [{'uid': 'A', 'side': 'LEFT'}])
report('冷启动博主难分秋色时随大众',
       result['side'] == 'RIGHT' and result['basis'] == 'crowd',
       f"side={result['side']} basis={result['basis']}")

# 案例 4：无战绩的新人保持中性 0.5，不能被当成 0 权重丢弃。
history = build(seed({'A': 'LEFT'}, winner='LEFT'))
newcomer = history.reliability('NEWCOMER')
report('无战绩新人权重为 0.5', abs(newcomer - 0.5) < 1e-9, f'weight={newcomer}')

# 案例 5：非冷启动下博主没有有效票时，仍应跟随大众，而不是抛硬币。
history = build(seed({'A': 'LEFT'}, winner='LEFT'))
result = history.choose(SIG_CURRENT, 100, 900, [])
report('无博主有效票时跟随大众',
       result['side'] == 'RIGHT',
       f"side={result['side']} scores={result['scores']} crowd={result['crowd_side']}")

# 案例 6：彻底没有信息（无博主票、大众也平分）时只能随机，但必须标注为 fallback。
history = build(seed({'A': 'LEFT'}, winner='LEFT'))
result = history.choose(SIG_CURRENT, 0, 0, [])
report('无任何方向信息时标注 fallback 并随机',
       result['basis'] == 'fallback' and result['random_tiebreak'] is True,
       f"side={result['side']} basis={result['basis']} tie={result['random_tiebreak']}")

# 案例 7：2026-09-30 17:45 的真实故障 —— 博主 9:1 指左、大众指右，旧实现把博主
# 整体压成 1 票与大众等权，打平后随机压了右。
history = build(seed({chr(ord('A') + i): 'LEFT' for i in range(10)}, winner='RIGHT'))
result = history.choose(SIG_CURRENT, 4917, 8740, [
    {'uid': chr(ord('A') + i), 'side': 'LEFT' if i < 9 else 'RIGHT'}
    for i in range(10)
])
report('博主 9:1 指左时不得因平局随机压右',
       result['side'] == 'LEFT',
       f"side={result['side']} scores={result['scores']} crowd={result['crowd_side']}")

if failures:
    logger.error(f'frog_oas check FAILED: {len(failures)} case(s) -> {failures}')
    sys.exit(1)
logger.info('frog_oas check: all cases passed')
