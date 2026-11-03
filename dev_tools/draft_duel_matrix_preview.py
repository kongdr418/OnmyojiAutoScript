"""打印协同对弈候选评分；默认使用示例名单，不读取或控制游戏。

python -B -m dev_tools.draft_duel_matrix_preview
python -B -m dev_tools.draft_duel_matrix_preview --own 平将门 云间不见岳 --offers 薰 莹草 鬼童丸
"""

import argparse
import json

from tasks.DraftDuel.strategy import plan_duel_lineup, rank_picks
from tasks.DraftDuel.matrix import score_team


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--own', nargs='*', default=['平将门', '云间不见岳'])
    parser.add_argument('--mate', nargs='*', default=['因幡辉夜姬', '灼华桃花妖', '百目鬼'])
    parser.add_argument('--offers', nargs='+', default=['薰', '茨木童子', '鬼童丸'])
    parser.add_argument('--json', action='store_true', help='输出方便对比的 JSON')
    args = parser.parse_args()
    ranked = rank_picks(args.offers, args.own, mate=args.mate)
    result = {'own': args.own, 'mate': args.mate, 'candidates': [
        {'name': p.name, 'score': round(p.score, 2),
         'parts': {k: round(v, 2) for k, v in p.score_parts},
         'lineup': p.lineup, 'reasons': p.reasons} for p in ranked]}
    if len(args.own) + 1 >= 3 and len(args.mate) >= 3:
        own, mate = plan_duel_lineup([*args.own, ranked[0].name], args.mate)
        score = score_team(own + mate, include_stats=True)
        result['field'] = {'own': own, 'mate': mate, 'score': round(score.total, 2),
                           'parts': {k: round(v, 2) for k, v in score.parts},
                           'details': score.details}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print('示例/传入名单离线评分，不代表当前实战画面。')
    print('我方已选：' + '、'.join(args.own))
    print('队友已选：' + ('、'.join(args.mate) or '未知'))
    for pick in ranked:
        print(f'\n{pick.name}：{pick.score:.2f} 分')
        print(' '.join(f'{label}{value:+.1f}' for label, value in pick.score_parts))
        print('预期上场：' + '、'.join(pick.lineup))
    if 'field' in result:
        field = result['field']
        print('\n最终上阵：我方 ' + '、'.join(field['own']) + ' / 队友 ' + '、'.join(field['mate']))
        print(f'阵容总分：{field["score"]:.2f}')


if __name__ == '__main__':
    main()
