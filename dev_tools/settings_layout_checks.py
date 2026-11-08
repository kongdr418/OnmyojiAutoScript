"""Settings layout API contract checks."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tasks.ActivityShikigami.config import ActivityShikigami
from tasks.MetaDemon.config import MetaDemon
from tasks.MetaDemonTicket.config import MetaDemonTicket
from module.config.config_model import ConfigModel


class SettingsLayoutChecks(unittest.TestCase):
    def test_legacy_client_dictionary_covers_activity_and_metademon_fields(self):
        # Legacy OASX clients translate row.name and do not consume schema titles/ui_text.
        from module.server.i18n import Addition
        translations = Addition.load_additions()['zh-CN']
        model = ConfigModel()
        for task in ('MetaDemon', 'MetaDemonTicket', 'ActivityShikigami'):
            for group, rows in model.script_task(task).items():
                self.assertIn(group, translations, f'{task}.{group}')
                for row in rows:
                    with self.subTest(task=task, group=group, field=row['name']):
                        label = translations.get(row['name'], '')
                        self.assertTrue(any('\u4e00' <= char <= '\u9fff' for char in label), label)

    def test_sources_reference_real_groups_and_fields(self):
        for name, model, attr in (
            ('ActivityShikigami', ActivityShikigami(), 'activity_shikigami'),
            ('MetaDemon', MetaDemon(), 'meta_demon'),
            ('MetaDemonTicket', MetaDemonTicket(), 'meta_demon_ticket'),
        ):
            payload = ConfigModel.script_task(SimpleNamespace(**{attr: model}), name)
            control = payload['settings_layout_config'][0]
            self.assertTrue(control['value'])
            self.assertTrue(control['ui_hidden'])
            self.assertEqual(control['ui_layout']['version'], 1)
            for section in control['ui_layout']['sections']:
                sources = list(section.get('groups', []))
                for variant in section.get('variants', []):
                    sources.extend(variant['groups'])
                for source in sources:
                    self.assertIn(source['name'], payload)
                    fields = {row['name'] for row in payload[source['name']]}
                    self.assertTrue(set(source.get('fields', [])) <= fields)
            model.settings_layout_config.merged_settings_enable = False
            restored = type(model).model_validate(model.model_dump())
            disabled = ConfigModel.script_task(SimpleNamespace(**{attr: restored}), name)
            self.assertFalse(disabled['settings_layout_config'][0]['value'])
            self.assertTrue(disabled['settings_layout_config'][0]['ui_hidden'])
            self.assertEqual(set(payload), set(disabled))
            self.assertEqual(control['ui_layout'], disabled['settings_layout_config'][0]['ui_layout'])

    def test_backend_owns_selection_rules_and_activity_form(self):
        for name, attr, model, group, field, expected in (
            ('MetaDemon', 'meta_demon', MetaDemon(), 'meta_demon_config', 'mode',
             [['鬼王战斗', '鬼王协助']]),
            ('ActivityShikigami', 'activity_shikigami', ActivityShikigami(), 'general_config', 'task_sequence',
             [['探索', '大富翁', '伪神']]),
        ):
            payload = ConfigModel.script_task(SimpleNamespace(**{attr: model}), name)
            row = next(row for row in payload[group] if row['name'] == field)
            self.assertEqual(row['ui_exclusive_groups'], expected)
            if name == 'ActivityShikigami':
                self.assertEqual(payload[group][0]['ui_form'][0]['type'], 'selection')
                self.assertIn('zh_CN', row['ui_text'])

    def test_single_exclusive_group_is_exposed_as_enum(self):
        """唯一互斥组覆盖全部选项时按单选下发，客户端才会渲染成下拉框。"""
        payload = ConfigModel.script_task(SimpleNamespace(meta_demon=MetaDemon()), 'MetaDemon')
        row = next(row for row in payload['meta_demon_config'] if row['name'] == 'mode')
        self.assertEqual(row['type'], 'enum')
        self.assertEqual(row['value'], '鬼王战斗')
        self.assertEqual(row['default'], '鬼王战斗')
        self.assertEqual(row['enumEnum'], ['鬼王战斗', '鬼王协助'])
        self.assertEqual(row['ui_exclusive_groups'], [['鬼王战斗', '鬼王协助']])
        # 互斥组只是子集的仍然保持多选。
        activity = ConfigModel.script_task(SimpleNamespace(activity_shikigami=ActivityShikigami()),
                                           'ActivityShikigami')
        seq = next(row for row in activity['general_config'] if row['name'] == 'task_sequence')
        self.assertEqual(seq['type'], 'multi_enum')

    def test_enum_shaped_mode_accepts_scalar_and_rejects_both(self):
        model = ConfigModel()
        from unittest.mock import patch
        with patch.object(ConfigModel, 'save'):
            self.assertTrue(model.script_set_arg('MetaDemon', 'meta_demon_config', 'mode', '鬼王协助'))
            self.assertEqual([m.value for m in model.meta_demon.meta_demon_config.mode], ['鬼王协助'])
            self.assertFalse(model.script_set_arg('MetaDemon', 'meta_demon_config', 'mode', '鬼王战斗,鬼王协助'))
            self.assertEqual([m.value for m in model.meta_demon.meta_demon_config.mode], ['鬼王协助'])

    def test_backend_dictionary_changes_are_returned_without_client_changes(self):
        model = MetaDemon()
        with patch('module.config.settings_presentation.Addition.load_additions', return_value={
                'zh-CN': {'preset_group': '后端新名称', 'preset_group_help': '后端新说明',
                          'low_star_battle_conf': '后端新分组'}}):
            payload = ConfigModel.script_task(SimpleNamespace(meta_demon=model), 'MetaDemon')
        row = next(row for row in payload['low_star_battle_conf'] if row['name'] == 'preset_group')
        self.assertEqual(row['ui_text']['zh_CN']['title'], '后端新名称')
        self.assertEqual(row['ui_text']['zh_CN']['description'], '后端新说明')
        self.assertEqual(payload['low_star_battle_conf'][0]['ui_group_title']['zh_CN'], '后端新分组')


if __name__ == '__main__':
    unittest.main()
