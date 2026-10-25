"""Read the shared version-2 JSON format and adapt it to the Chess runtime."""
from pathlib import Path
from tasks.Chess.strategy.shikigami_catalog import build_lineup_shikigami
from tasks.Chess.strategy.custom_lineups import CustomLineup, EMBLEMS, lineup_bond_counts, read_collection

LINEUPS_FILE = Path(__file__).with_name('lineup_strategy.json')


def strategy_from_document(document, prefix=''):
    document = CustomLineup.model_validate(document) if isinstance(document, dict) else document
    shikigami = build_lineup_shikigami({
        slot.shikigami: {'position': index, 'weight': slot.weight,
                         'souls': [item.key for item in slot.equipment if item.kind == 'soul']}
        for index, slot in enumerate(document.slots, 1) if slot.shikigami})
    for slot in document.slots:
        if slot.shikigami:
            shikigami[slot.shikigami]['preferred_emblems'] = tuple(
                EMBLEMS[item.key]['name'] for item in slot.equipment if item.kind == 'emblem')
    return {'key': prefix + document.key, 'display_name': document.display_name,
            'shikigami': shikigami,
            'primary_bond': document.primary_bond or lineup_bond_counts(document.slots).most_common(1)[0][0],
            'secondary_bond': document.secondary_bond,
            'hakuzosu_protect_position': document.hakuzosu_protect_position,
            'arakawa_goldfish_position': document.arakawa_goldfish_position}


def load_lineup_registry(path=None):
    return {key: {'display_name': raw['display_name'], 'strategy': strategy_from_document(raw)}
            for key, raw in read_collection(path or LINEUPS_FILE).items()}
