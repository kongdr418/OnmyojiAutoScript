"""Settings layout API contract checks."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tasks.ActivityShikigami.config import ActivityShikigami
from tasks.MetaDemon.config import MetaDemon
from module.config.config_model import ConfigModel


class SettingsLayoutChecks(unittest.TestCase):
    def test_legacy_client_dictionary_covers_activity_and_metademon_fields(self):
        # Legacy OASX clients translate row.name and do not consume schema titles/ui_text.
        from module.server.i18n import Addition
        translations = Addition.load_additions()['zh-CN']
        model = ConfigModel()
        for task in ('MetaDemon', 'ActivityShikigami'):
            for group, rows in model.script_task(task).items():
                self.assertIn(group, translations, f'{task}.{group}')
                for row in rows:
                    with self.subTest(task=task, group=group, field=row['name']):
                        label = translations.get(row['name'], '')
                        self.assertTrue(any('\u4e00' <= char <= '\u9fff' for char in label), label)

    def test_sources_reference_real_groups_and_fields(self):
        for name, model in (('ActivityShikigami', ActivityShikigami()), ('MetaDemon', MetaDemon())):
            attr = 'activity_shikigami' if name == 'ActivityShikigami' else 'meta_demon'
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
            ('ActivityShikigami', 'activity_shikigami', ActivityShikigami(), 'general_config', 'task_sequence',
             [['探索', '大富翁', '伪神']]),
        ):
            payload = ConfigModel.script_task(SimpleNamespace(**{attr: model}), name)
            row = next(row for row in payload[group] if row['name'] == field)
            self.assertEqual(row['ui_exclusive_groups'], expected)
            if name == 'ActivityShikigami':
                self.assertEqual(payload[group][0]['ui_form'][0]['type'], 'selection')
                self.assertIn('zh_CN', row['ui_text'])

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
