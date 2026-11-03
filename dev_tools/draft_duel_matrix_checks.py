"""离线验证矩阵决策，不需要模拟器、OCR 或第三方依赖。

运行：python -B -m dev_tools.draft_duel_matrix_checks -v
"""

import importlib.util
import json
from itertools import combinations
from pathlib import Path
import random
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tasks.DraftDuel import strategy as s
from tasks.DraftDuel.matrix import ScoringMatrix, best_plan, legal_lineup, score_team


def catalog_for(roles):
    return {name: s.Entry(name, '', role, '', '', '', '', '', 0)
            for name, role in roles.items()}


def load_runtime(filename):
    """只替换设备无关测试不需要的 UI 日志、计时器和配置依赖。"""
    path = Path(s.__file__).parent / 'runtime' / filename
    spec = importlib.util.spec_from_file_location('offline_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules', {
        'module.logger': SimpleNamespace(logger=Mock()),
        'module.base.timer': SimpleNamespace(Timer=Mock()),
        'tasks.DraftDuel.config': SimpleNamespace(UserStatus=SimpleNamespace(MEMBER='member')),
    }):
        spec.loader.exec_module(module)
    return module


class MatrixScoringTests(unittest.TestCase):
    def test_user_pair_overrides_sources_and_is_symmetric(self):
        names = ['初音未来', '鲸汐千姬']
        matrix = ScoringMatrix(names)
        self.assertEqual(matrix.pairs[frozenset(names)], 22)
        self.assertEqual(dict(matrix.score(names).parts)['搭配'], 22)
        self.assertEqual(matrix.score(names).total, matrix.score(reversed(names)).total)

    def test_three_card_bonus_requires_all_three(self):
        matrix = ScoringMatrix(['猫川', '夏目猫老师', '兵勇'])
        self.assertEqual(dict(matrix.score(['猫川', '夏目猫老师']).parts)['搭配'], 14)
        self.assertEqual(dict(matrix.score(matrix.abilities).parts)['搭配'], 14 + 12 + 8)

    def test_negative_pair_is_retained(self):
        self.assertEqual(dict(score_team(['帝释天', '纺愿缘结神']).parts)['搭配'], -8)

    def test_fire_quality_uses_actual_tags(self):
        matrix = ScoringMatrix(['初音未来', '晨辉惠比寿', '辉夜姬'])
        self.assertEqual(matrix.abilities['初音未来'].fire_units, 0)
        self.assertLess(matrix.abilities['晨辉惠比寿'].fire_units,
                        matrix.abilities['辉夜姬'].fire_units)

    def test_one_amplifier_does_not_fill_two_independent_slots(self):
        catalog = catalog_for({'云间不见岳': '增伤辅助'})
        matrix = ScoringMatrix(catalog, catalog)
        self.assertTrue(matrix.abilities['云间不见岳'].amplify)
        # 已替保护位，但仍缺一个供火位和一个功能位。
        self.assertEqual(dict(matrix.score(catalog, expected_size=1).parts)['缺口'], -26)

    def test_missing_partner_penalty_disappears_when_partner_is_present(self):
        self.assertLess(dict(score_team(['初音未来']).parts)['条件'], 0)
        self.assertEqual(dict(score_team(['初音未来', '鲸汐千姬']).parts)['条件'], 0)

    def test_invalid_names_duplicates_and_expected_size_are_rejected(self):
        with self.assertRaises(ValueError):
            score_team(['没有这个式神'])
        with self.assertRaises(ValueError):
            score_team(['辉夜姬', '辉夜姬'])
        with self.assertRaises(ValueError):
            score_team(['辉夜姬'], expected_size=0)


class MatrixPlanningTests(unittest.TestCase):
    def setUp(self):
        self.own = ['平将门', '云间不见岳', '因幡辉夜姬', '灼华桃花妖', '薰']
        self.mate = ['鲸汐千姬', '初音未来', '茨木童子', '神无月', '百目鬼']

    def test_joint_plan_is_global_maximum_of_all_legal_combinations(self):
        a, b = s.plan_duel_lineup(self.own, self.mate)
        best = max(score_team(x + y).total
                   for x in combinations(self.own, 3) for y in combinations(self.mate, 3)
                   if legal_lineup(x + y))
        self.assertAlmostEqual(score_team(a + b).total, best)
        self.assertEqual((len(a), len(b)), (3, 3))

    def test_joint_plan_swaps_exactly_with_sides_including_equal_scores(self):
        catalog = catalog_for({n: '' for n in '甲乙丙丁戊己庚辛壬癸'})
        own, mate = tuple(catalog)[:5], tuple(catalog)[5:]
        a, b = s.plan_duel_lineup(own, mate, catalog=catalog)
        c, d = s.plan_duel_lineup(mate, own, catalog=catalog)
        self.assertEqual((a, b), (d, c))

    def test_stats_reward_both_sides_and_preserve_symmetry(self):
        table = {'薰': (10, 0), '初音未来': (0, 10), '神无月': (8, 2)}
        with patch.object(s.duel_stats, '_tally', return_value=table):
            a, b = s.plan_duel_lineup(self.own, self.mate)
            c, d = s.plan_duel_lineup(self.mate, self.own)
            self.assertEqual((a, b), (d, c))
            expected = max(score_team(x + y, include_stats=True).total
                           for x in combinations(self.own, 3)
                           for y in combinations(self.mate, 3) if legal_lineup(x + y))
            self.assertAlmostEqual(score_team(a + b, include_stats=True).total, expected)

    def test_locked_members_and_summon_slot_constraints(self):
        a, b = s.plan_duel_lineup(self.own, self.mate, locked=['薰'])
        self.assertIn('薰', a)
        self.assertTrue(legal_lineup(a + b))
        with self.assertRaises(ValueError):
            s.plan_duel_lineup(self.own, self.mate, locked=['不存在'])
        with self.assertRaises(ValueError):
            s.plan_duel_lineup(['猫川', '千姬', '辉夜姬'], self.mate)

    def test_soft_formula_allows_a_stronger_no_fire_lineup(self):
        catalog = catalog_for({'甲': '输出', '乙': '输出', '丙': '控制', '丁': '火机',
                               '戊': '输出', '己': '输出', '庚': '治疗', '辛': '火机'})
        with patch.object(s, 'strength_points', side_effect=lambda entry, *a, **k:
                          0 if entry.name in '丁辛' else 1000):
            own, mate = s.plan_duel_lineup('甲乙丙丁', '戊己庚辛', catalog=catalog)
        self.assertNotIn('丁', own)
        self.assertNotIn('辛', mate)

    def test_random_pools_remain_legal_symmetric_and_order_independent(self):
        rng = random.Random(7)
        names = list(s.CATALOG)
        for _ in range(20):
            pool = rng.sample(names, 10)
            own, mate = pool[:5], pool[5:]
            a, b = s.plan_duel_lineup(own, mate)
            c, d = s.plan_duel_lineup(reversed(mate), reversed(own))
            self.assertEqual((a, b), (d, c))
            self.assertTrue(legal_lineup(a + b))

    def test_single_side_fallback_respects_fire_cap_and_locked_member(self):
        lineup = s.plan_lineup(self.own, team_size=3, locked=['薰'], max_fire=1)
        self.assertIn('薰', lineup)
        self.assertEqual(sum(s.is_fire_supplier(n) for n in lineup), 1)

    def test_score_is_independent_of_floating_point_summation_order(self):
        names = self.own[:3] + self.mate[:3]
        baseline = score_team(names).total
        rng = random.Random(15)
        for _ in range(15):
            rng.shuffle(names)
            self.assertEqual(score_team(names).total, baseline)


class MatrixPickTests(unittest.TestCase):
    def test_all_known_offers_are_ranked_and_parts_sum_to_total(self):
        ranked = s.rank_picks(['须佐之男', '薰', '初音未来', 'OCR无法识别'], ['平将门'])
        self.assertEqual({p.name for p in ranked}, {'须佐之男', '薰', '初音未来'})
        for pick in ranked:
            self.assertAlmostEqual(pick.score, sum(value for _, value in pick.score_parts))
        self.assertEqual(s.choose_pick(['须佐之男', '薰', '初音未来'], ['平将门']), ranked[0])

    def test_draft_uses_three_field_slots_instead_of_all_five_picks(self):
        team = ['平将门', '云间不见岳', '因幡辉夜姬', '灼华桃花妖']
        for pick in s.rank_picks(['薰', '莹草', '鬼童丸'], team):
            self.assertEqual(len(pick.lineup), 3)
            self.assertTrue(set(pick.lineup) <= set((*team, pick.name)))

    def test_teammate_coverage_changes_pick_instead_of_forcing_own_fire(self):
        catalog = catalog_for({'甲': '输出', '乙': '治疗', '丙': '火机',
                               '丁': '输出', '戊': '火机', '己': '火机', '庚': '控制'})
        alone = s.choose_pick('丙丁', '甲乙', catalog=catalog)
        joint = s.choose_pick('丙丁', '甲乙', mate='戊己庚', catalog=catalog)
        self.assertEqual(alone.name, '丙')
        self.assertEqual(joint.name, '丁')

    def test_unknown_empty_or_full_inputs_have_clear_errors(self):
        for offers, team, size in [(['未识别'], [], 5), (['薰'], ['未知'], 5),
                                    (['薰'], ['平将门'], 1)]:
            with self.assertRaises(ValueError):
                s.choose_pick(offers, team, team_size=size)

    def test_conflicting_reserve_cards_do_not_block_later_draft_rounds(self):
        for pick in s.rank_picks(['薰', '平将门', '鬼童丸'], ['猫川', '千姬']):
            self.assertTrue(legal_lineup(pick.lineup))
            self.assertIn(pick.name, pick.lineup)
        # 双方第一手是同名卡，仍可选择下一张，不丢掉整份队友信息。
        ranked = s.rank_picks(['薰', '平将门'], ['辉夜姬'], mate=['辉夜姬'])
        self.assertEqual(len(ranked), 2)
        self.assertTrue(all(legal_lineup(p.lineup) for p in ranked))


class RuntimeMatrixTests(unittest.TestCase):
    def test_progress_exchange_rejects_previous_match_and_malformed_file(self):
        module = load_runtime('cooperation.py')
        captain = module.DraftDuelCooperationMixin()
        captain._configured_role = lambda: 'captain'
        member = module.DraftDuelCooperationMixin()
        member._configured_role = lambda: 'member'
        captain._draft_started_at = member._draft_started_at = time.time()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory, patch.object(
                module, 'DUEL_SHARE_DIR', Path(directory)):
            captain._draft_share_write(['辉夜姬', '薰'])
            self.assertEqual(member._draft_share_read_mate(), ['辉夜姬', '薰'])
            path = Path(directory) / 'draft_progress_captain.json'
            self.assertFalse(path.with_suffix('.tmp').exists())
            self.assertFalse((Path(directory) / 'draft_share_captain.json').exists())
            path.write_text(json.dumps({'names': ['辉夜姬'], 'ts': 1}), encoding='utf-8')
            self.assertEqual(member._draft_share_read_mate(), [])
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(member._draft_share_read_mate(), [])

    def test_recommendation_switch_and_wait_window_are_unchanged(self):
        module = load_runtime('selection.py')
        runtime = module.DraftDuelSelectionMixin()
        runtime.RECOMMEND_LOW = 22
        config = SimpleNamespace(use_teammate_recommendation=False)
        runtime.config = SimpleNamespace(draft_duel=SimpleNamespace(draft_duel_config=config))
        runtime._choose_by_strategy = Mock(return_value=2)
        runtime._recommended_slot = Mock(return_value=None)
        self.assertEqual(runtime._choose_draft_slot(30), 2)
        runtime._recommended_slot.assert_not_called()
        config.use_teammate_recommendation = True
        self.assertIsNone(runtime._choose_draft_slot(23))
        self.assertEqual(runtime._choose_draft_slot(22), 2)
        runtime._recommended_slot.return_value = 1
        self.assertEqual(runtime._choose_draft_slot(29), 1)

    def test_runtime_ranks_offers_with_teammate_and_maps_back_to_slot(self):
        module = load_runtime('selection.py')
        runtime = module.DraftDuelSelectionMixin()
        runtime._team = ['平将门', 'OCR未知']
        runtime.TOTAL_PICKS = 5
        offers = ['薰', '百目鬼', '茨木童子']
        runtime._role_name = lambda index: offers[index]
        runtime._draft_share_read_mate = lambda: ['因幡辉夜姬', '灼华桃花妖']
        expected = s.choose_pick(offers, ['平将门'], mate=runtime._draft_share_read_mate())
        self.assertEqual(runtime._choose_by_strategy(), offers.index(expected.name))


if __name__ == '__main__':
    unittest.main()
