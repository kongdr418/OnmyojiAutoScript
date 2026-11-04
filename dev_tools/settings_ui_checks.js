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
for (const [task, payload] of Object.entries(payloads)) {
    const groups = [];
    const context = vm.createContext({settingsPayload: payload, renderedGroups: [],
        createGroup(name, title, rows, variants) { groups.push({name, title, rows, variants}); }});
    vm.runInContext(functions, context);
    context.renderSettings();
    assert.equal(groups.length, task === 'MetaDemon' ? 5 : 4);
    assert(groups.every(group => group.rows.length && group.title));
    for (const group of groups) {
        for (const row of group.rows) {
            assert(!row.ui_hidden);
            assert(payload[row.sourceGroup].some(original => original.name === row.name));
        }
        for (const variant of group.variants || []) {
            const rows = context.rowsForSources(variant.groups);
            assert(rows.length > 0);
            assert(rows.every(row => payload[row.sourceGroup].some(original => original.name === row.name)));
        }
    }
    const control = payload.settings_layout_config[0];
    control.value = false;
    groups.length = 0;
    context.renderSettings();
    assert(!groups.some(group => group.name === 'settings_layout_config'));
    assert(groups.length > (task === 'MetaDemon' ? 5 : 4));
    const row = {value: ['探索', '爬塔'], ui_exclusive_groups: [['探索', '大富翁', '伪神']]};
    assert.deepEqual(Array.from(context.selectionValues(row, '大富翁', true)), ['爬塔', '大富翁']);
    assert.deepEqual(Array.from(context.selectionValues(row, '探索', false)), ['爬塔']);
}
console.log('PASS: settings sections, all variants, source-field mapping, hidden controls, flat fallback and exclusive selections');
