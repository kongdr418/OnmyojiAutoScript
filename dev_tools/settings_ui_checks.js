// Exercise the actual QML settings functions against real backend payloads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');

const result = spawnSync('toolkit/python.exe', ['-c', `
import json
from module.config.config_model import ConfigModel
model = ConfigModel()
print('PAYLOAD:' + json.dumps({name: model.script_task(name) for name in ('MetaDemon','ActivityShikigami')}, ensure_ascii=False, default=str))
`], {encoding: 'utf8', env: {...process.env, PYTHONIOENCODING: 'utf-8'}});
assert.equal(result.status, 0, result.stderr);
const payloads = JSON.parse(result.stdout.split('PAYLOAD:').pop());
const qml = fs.readFileSync('module/gui/qml/Component/Args.qml', 'utf8');
const functions = qml.slice(qml.indexOf('    function localized('), qml.indexOf('    function set_task('));
for (const [task, originalPayload] of Object.entries(payloads)) {
  for (const uiLocale of ['zh_CN', 'en_US']) {
    const payload = structuredClone(originalPayload);
    const groups = [];
    const context = vm.createContext({settingsPayload: payload, renderedGroups: [], uiLocale,
        createGroup(name, title, rows, variants) { groups.push({name, title, rows, variants}); }});
    vm.runInContext(functions, context);
    context.renderSettings();
    const control = payload.settings_layout_config[0];
    assert.equal(groups.length, control.ui_layout.sections.length);
    assert(groups.every(group => group.rows.length && group.title));
    const visibleFields = new Set();
    for (const group of groups) {
        for (const row of group.rows) {
            assert(!row.ui_hidden);
            assert(payload[row.sourceGroup].some(original => original.name === row.name));
            visibleFields.add(`${row.sourceGroup}.${row.name}`);
        }
        for (const variant of group.variants || []) {
            const rows = context.rowsForSources(variant.groups);
            assert(rows.length > 0);
            assert(rows.every(row => payload[row.sourceGroup].some(original => original.name === row.name)));
            rows.forEach(row => visibleFields.add(`${row.sourceGroup}.${row.name}`));
        }
    }
    for (const [source, rows] of Object.entries(payload)) {
        rows.filter(row => !row.ui_hidden).forEach(row =>
            assert(visibleFields.has(`${source}.${row.name}`), `${task}: missing ${source}.${row.name}`));
    }
    const translatedRow = context.rowsForSources([{name: 'meta_demon_config'}]).find(row => row.name === 'mode');
    if (translatedRow) {
        const original = payload.meta_demon_config.find(row => row.name === 'mode');
        assert.equal(translatedRow.title, original.ui_text[uiLocale].title);
        assert.deepEqual(Array.from(translatedRow.options), original.enumEnum);
        assert.deepEqual(Array.from(translatedRow.optionLabels), original.enumEnum.map(option => original.ui_text[uiLocale].options[option]));
    }
    control.value = false;
    groups.length = 0;
    context.renderSettings();
    assert(!groups.some(group => group.name === 'settings_layout_config'));
    assert(groups.length > control.ui_layout.sections.length);
    const row = {value: ['探索', '爬塔'], ui_exclusive_groups: [['探索', '大富翁', '伪神']]};
    assert.deepEqual(Array.from(context.selectionValues(row, '大富翁', true)), ['爬塔', '大富翁']);
    assert.deepEqual(Array.from(context.selectionValues(row, '探索', false)), ['爬塔']);
    if (translatedRow) {
        const mode = payload.meta_demon_config.find(row => row.name === 'mode');
        const selected = context.selectionValues({...mode, value: ['鬼王战斗', '门票刷取：探索']}, '鬼王协助', true);
        assert.deepEqual(Array.from(selected), ['门票刷取：探索', '鬼王协助']);
    }
  }
}
console.log('PASS: backend-defined sections, complete field coverage, variants, source-field mapping, bilingual labels, raw option values, hidden controls and exclusive selections');
