"""Offline checks for task selection and fixed priorities."""
import unittest
import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import datetime
from pydantic import ValidationError
from tasks.MetaDemon.config import MetaDemonConfig, MetaDemonMode as Mode
from tasks.ActivityShikigami.config import GeneralConfig

class SelectionChecks(unittest.TestCase):
    def test_meta_default_and_legacy(self):
        self.assertEqual(MetaDemonConfig().mode, [Mode.BOSS_BATTLE])
        self.assertEqual(MetaDemonConfig(mode=Mode.AWAKE.value).mode, [Mode.AWAKE])
        self.assertEqual(MetaDemonConfig(mode=[]).mode, [])


    def test_meta_ticket_exclusivity_and_priority(self):
        self.assertEqual(MetaDemonConfig(mode=[Mode.BOSS_BATTLE, Mode.EXPLORATION]).mode,
                         [Mode.EXPLORATION, Mode.BOSS_BATTLE])
        with self.assertRaises(ValidationError):
            MetaDemonConfig(mode=[Mode.AWAKE, Mode.EXPLORATION])

    def test_activity_defaults_only_climb_and_easy(self):
        from tasks.ActivityShikigami.config import ActivityTask, ClimbTask
        conf = GeneralConfig()
        self.assertEqual(conf.task_sequence, [ActivityTask.CLIMB])
        self.assertEqual(conf.climb_task_sequence, [ClimbTask.EASY])

    def test_activity_order_and_legacy_exclusivity(self):
        conf = GeneralConfig(task_sequence=['首领', '爬塔', '伪神'],
                             climb_task_sequence=['百体', '体力', '简单', '困难'],
                             pass_limit='3,4', ap_limit=2, ap100_limit=1,
                             boss_limit=1, fakegod_limit=1)
        self.assertEqual(conf.task_sequence_v, ['伪神', '爬塔', '首领'])
        self.assertEqual(conf.climb_sequence_v, ['pass_hard', 'pass_easy', 'ap', 'ap100'])
        conf = GeneralConfig(task_sequence=['伪神', '大富翁', '探索', '爬塔', '首领'],
                             climb_task_sequence=[], throw_limit=1, fakegod_limit=1, boss_limit=1)
        self.assertEqual(conf.task_sequence_v, ['探索', '首领'])

class DispatchChecks(unittest.TestCase):
    def test_all_runtime_rules_are_generated_including_profiled_clicks(self):
        from tasks.ActivityShikigami.script_task import ScriptTask
        root = Path(__file__).resolve().parents[1] / 'tasks/ActivityShikigami'
        for path in root.rglob('*.py'):
            if path.name == 'assets.py':
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == 'self' and node.attr.startswith(('I_', 'O_', 'C_', 'S_'))):
                    self.assertTrue(hasattr(ScriptTask, node.attr), f'{path.name}:{node.lineno} {node.attr}')

    def test_activity_dispatch_uses_selected_order_and_soft_time_limit(self):
        from tasks.ActivityShikigami.script_task import ScriptTask
        owner = object.__new__(ScriptTask)
        owner.__dict__['conf'] = SimpleNamespace(general_config=GeneralConfig(
            task_sequence=['首领', '爬塔', '大富翁'], climb_task_sequence=['体力'],
            throw_limit=1, ap_limit=2, boss_limit=1))
        events = []
        owner.before_run = Mock()
        owner.finish_activity_task = Mock()
        owner.time_limit_reached = Mock(side_effect=[False, False, True])
        owner.run_rich_man = lambda: events.append('rich_man')
        owner.run_climb = lambda: events.append('climb')
        owner.run_boss = lambda: events.append('boss')
        owner.run()
        self.assertEqual(events, ['rich_man', 'climb'])
        owner.finish_activity_task.assert_called_once()

    def test_old_climb_branches_counts_and_battle_presets_migrate(self):
        from tasks.ActivityShikigami.config import ActivityShikigami
        conf = ActivityShikigami(general_climb={'run_sequence':'ap100,pass,ap,boss',
            'pass_limit':12, 'ap_limit':20, 'ap100_limit':3, 'boss_limit':2},
            switch_soul_config={'enable_switch_ap':True, 'ap_group_team':'2,3'},
            pass_battle_conf={'preset_enable':True,'preset_group':3,'preset_team':2})
        self.assertEqual(conf.general_config.task_sequence_v, ['爬塔','首领'])
        self.assertEqual(conf.general_config.climb_sequence_v, ['pass_easy','ap','ap100'])
        self.assertEqual(conf.general_config.pass_limits_v, (12, 0))
        self.assertEqual(conf.switch_soul_config.ap_group_team, '2,3')
        self.assertEqual(conf.pass_battle_conf.preset_group, 3)
        self.assertNotIn('general_climb', conf.model_dump())

    def test_api_saves_canonical_multi_select_and_rejects_invalid_input(self):
        from module.config.config_model import ConfigModel
        model = ConfigModel()
        with patch.object(ConfigModel, 'save') as save:
            self.assertTrue(model.script_set_arg('ActivityShikigami','general_config','task_sequence', '["首领","爬塔"]'))
            self.assertEqual([item.value for item in model.activity_shikigami.general_config.task_sequence], ['爬塔','首领'])
            before = model.activity_shikigami.general_config.model_dump()
            self.assertFalse(model.script_set_arg('ActivityShikigami','general_config','task_sequence', '["不存在"]'))
            self.assertEqual(model.activity_shikigami.general_config.model_dump(), before)
            self.assertEqual(save.call_count, 1)

    def test_farming_executes_before_selected_boss(self):
        events = []
        class Boss:
            def run(self):
                events.append('boss')
        path = Path(__file__).resolve().parents[1] / 'tasks/MetaDemon/script_task.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        ns = dict(BossBattle=Boss, MetaDemonMode=Mode, datetime=datetime, logger=Mock())
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), ns)
        runner = ns['ScriptTask']()
        runner.config = SimpleNamespace(meta_demon=SimpleNamespace(meta_demon_config=
            MetaDemonConfig(mode=[Mode.BOSS_BATTLE, Mode.AWAKE])))
        fake = SimpleNamespace(run_ticket_farming=lambda owner, mode: events.append(mode) or True)
        with patch.dict(sys.modules, {'tasks.MetaDemon.ticket_farming': fake}):
            runner.run()
        self.assertEqual(events, [Mode.AWAKE, 'boss'])

if __name__ == '__main__':
    unittest.main()
