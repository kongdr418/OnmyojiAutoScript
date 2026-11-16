"""Locate the live CPython 3.11 drawing singleton in MuMu guest RAM."""
import struct
import time
import json
import math
from datetime import datetime, timezone

import numpy as np

from module.scrolls.client import progress_rows
from module.scrolls import storage


_locations = {}


class LiveRoleMismatch(ValueError):
    """A validated live player object belongs to a different selected role."""
    def __init__(self, message, actual_name='', actual_role_id=''):
        super().__init__(message)
        self.actual_name = actual_name
        self.actual_role_id = actual_role_id


class LiveCacheUnavailable(ValueError):
    """The live identity is valid, but its drawing data is not ready."""


def _location_path(key):
    # Locations describe a class in one live game process, not a credential or
    # an activity. The selected role and current issue are revalidated on read.
    digest = storage.storage_key(json.dumps(key[:3], separators=(',', ':')))
    return storage.ROOT / 'live-locations' / (digest + '.json')


def _load_location(key, memory):
    if key is None or memory is None:
        return None
    try:
        path = _location_path(key)
        if path.stat().st_size > 1024:
            return None
        value = json.loads(path.read_text('utf-8'))
        if not isinstance(value, dict):
            return None
        region, root, manager = value['location']
        base, size = region
        low = getattr(memory, 'low_regions', ())
        ranges = [(GuestObjects.physical_base, base, size), *(low if isinstance(low, (list, tuple)) else ())]
        if (value.get('version') != 1 or any(type(item) is not int for item in (base, size, root, manager))
                or tuple(region) not in memory.regions or root % 4096
                or not any(start <= root <= start + length - 4096 for start, _, length in ranges)
                or not 0 < manager < 1 << 47 or manager % 8):
            return None
        return tuple(region), root, manager
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_location(key, location):
    if key is not None:
        try:
            storage.atomic_write(_location_path(key), json.dumps({'version': 1, 'location': location}).encode())
        except OSError:
            pass  # An unwritable pointer cache must not invalidate a live read.


def _forget_location(key):
    _locations.pop(key, None)
    if key is not None:
        try:
            _location_path(key).unlink(missing_ok=True)
        except OSError:
            pass


class GuestObjects:
    # Main MuMu RAM starts at 2 MiB; lower RAM has separately discovered host
    # mappings. Validate every translation and live object before using it.
    physical_base = 0x200000
    page_mask = 0x000ffffffffff000
    manager_name = b'HuiJuanInfoMgr\0'

    def __init__(self, memory, region, deadline):
        self.memory = memory
        self.host_base, self.ram_size = region
        low = getattr(memory, 'low_regions', ())
        self.low_regions = tuple(low) if isinstance(low, (list, tuple)) else ()
        self.deadline = deadline
        self.root = None
        self.table_pages = {}
        self.data_pages = {}
        self.reference_pages = None
        self.reference_locations = None
        self.field_cache = {}

    def check_time(self):
        if time.monotonic() > self.deadline:
            raise TimeoutError('读取绘卷内存超时，请稍后重试')

    def physical(self, address, size):
        self.check_time()
        for physical, host, length in self.ram_ranges():
            if physical <= address and address + size <= physical + length:
                return self.memory.read(host + address - physical, size)
        raise ValueError('Guest address outside supported RAM')

    def ram_ranges(self):
        return [(self.physical_base, self.host_base, self.ram_size), *self.low_regions]

    def ram_chunks(self):
        for base, _, size in self.ram_ranges():
            for offset in range(0, size, 32 * 1024**2):
                yield base + offset, min(32 * 1024**2, size - offset)

    def scan(self, needles):
        result = {needle: set() for needle in needles}
        overlap = max(map(len, needles)) - 1
        for base, _, size in self.ram_ranges():
            tail = b''
            for offset in range(0, size, 32 * 1024**2):
                data = tail + self.physical(base + offset, min(32 * 1024**2, size - offset))
                for needle in needles:
                    cursor = 0
                    while True:
                        at = data.find(needle, cursor)
                        if at < 0:
                            break
                        cursor = at + 1
                        result[needle].add(base + offset - len(tail) + at)
                tail = data[-overlap:] if overlap else b''
        return result

    def roots(self, address):
        paths = [(address, [])]
        for _ in range(4):
            pages = {location & ~4095 for location, _ in paths}
            if not pages:
                return []
            matches = self.page_references(pages)
            following = []
            for location, indexes in paths:
                page = location & ~4095
                for entry_address in matches[page]:
                    following.append((entry_address, indexes + [entry_address % 4096 // 8]))
            paths = following
            if len(paths) > 64:
                raise ValueError('Ambiguous guest page tables')
        return [(location & ~4095, (address & 4095) + sum(index << (12 + 9 * level)
                                                       for level, index in enumerate(indexes)))
                for location, indexes in paths]

    def page_references(self, pages):
        if self.reference_pages is None:
            self.index_page_entries()
        return {page: self.reference_locations[self.reference_pages == page].tolist() for page in pages}

    def index_page_entries(self):
        page_chunks, location_chunks = [], []
        for address, size in self.ram_chunks():
            data = self.physical(address, size)
            # Retain aligned, present user entries referencing mapped RAM.
            # This index serves all four reverse-walk levels in one RAM pass.
            entries = np.frombuffer(data, dtype='<u8')
            frames = entries & self.page_mask
            valid = ((entries & 0x85) == 5) & ((entries & 0x7ff0000000000000) == 0)
            mapped = np.zeros(len(frames), dtype=bool)
            for start, _, length in self.ram_ranges():
                mapped |= (frames >= start) & (frames < start + length)
            valid &= mapped
            indexes = np.flatnonzero(valid)
            page_chunks.append(frames[indexes])
            location_chunks.append(address + indexes.astype(np.uint64) * 8)
        self.reference_pages = np.concatenate(page_chunks)
        self.reference_locations = np.concatenate(location_chunks)

    def translate(self, address):
        if not 0 < address < 1 << 47:
            raise ValueError('Invalid guest user address')
        table = self.root
        for level in (4, 3, 2, 1):
            if table not in self.table_pages:
                self.table_pages[table] = self.physical(table, 4096)
            offset = ((address >> (12 + 9 * (level - 1))) & 511) * 8
            entry = struct.unpack_from('<Q', self.table_pages[table], offset)[0]
            if entry & 5 != 5:
                raise ValueError('Guest user page is unmapped')
            table = entry & self.page_mask
            if level == 1:
                return table + (address & 4095)
            if entry & 128:
                mask = (1 << (12 + 9 * (level - 1))) - 1
                return (table & ~mask) + (address & mask)

    def read(self, address, size):
        result = bytearray()
        while size:
            take = min(size, 4096 - (address & 4095))
            page = address & ~4095
            if page not in self.data_pages:
                self.data_pages[page] = self.physical(self.translate(page), 4096)
            offset = address & 4095
            result.extend(self.data_pages[page][offset:offset + take])
            address += take
            size -= take
        return bytes(result)

    def pointer(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def typename(self, address):
        name = self.pointer(self.pointer(address + 8) + 24)
        return self.read(name, 80).split(b'\0', 1)[0].decode('ascii')

    def page_mapping(self):
        result = {}

        def collect(table, level, base):
            self.check_time()
            if not any(start <= table <= start + length - 4096 for start, _, length in self.ram_ranges()):
                return
            for index, entry in enumerate(struct.unpack('<512Q', self.physical(table, 4096))):
                virtual = base + (index << (12 + 9 * (level - 1)))
                if entry & 5 != 5 or virtual >= 1 << 47:
                    continue
                physical = entry & self.page_mask
                if level == 1:
                    result.setdefault(physical, []).append(virtual)
                elif entry & 128:
                    for offset in range(0, 1 << (12 + 9 * (level - 1)), 4096):
                        result.setdefault(physical + offset, []).append(virtual + offset)
                else:
                    collect(physical, level - 1, virtual)

        collect(self.root, 4, 0)
        return result

    def dictionary(self, address, *, wanted=None):
        if self.typename(address) != 'dict':
            raise ValueError('Expected Python dictionary')
        return self.entries(self.pointer(address + 32), self.pointer(address + 40), wanted=wanted)

    def entries(self, keys, values=0, *, wanted=None):
        header = self.read(keys, 32)
        logsize, logbytes, kind = struct.unpack_from('<BBB', header, 8)
        size = struct.unpack_from('<q', header, 24)[0]
        if not 0 <= size <= 8192 or not 0 <= logsize <= 15 or not logsize <= logbytes <= logsize + 3 or kind not in (0, 1, 2):
            raise ValueError('Unsupported Python dictionary layout')
        entries = keys + 32 + (1 << logbytes)
        result = {}
        wanted = set(wanted) if wanted is not None else None
        cache = getattr(self, 'field_cache', None)
        if cache is None:
            self.field_cache = cache = {}
        cache_key = (keys, frozenset(wanted)) if wanted is not None else None
        if cache_key in cache:
            try:
                for name, index in cache[cache_key].items():
                    if not 0 <= index < size:
                        raise ValueError('Cached dictionary field moved')
                    item = entries + index * (16 if kind else 24)
                    key = self.pointer(item if kind else item + 8)
                    value = self.pointer(values + index * 8) if values else self.pointer(item + (8 if kind else 16))
                    if not key or not value or self.object(key) != name:
                        raise ValueError('Cached dictionary field changed')
                    result[name] = value
                if wanted <= result.keys():
                    return result
            except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
                pass
            cache.pop(cache_key, None)
            result.clear()
        positions = {}
        for index in range(size):
            item = entries + index * (16 if kind else 24)
            key = self.pointer(item if kind else item + 8)
            value = self.pointer(values + index * 8) if values else self.pointer(item + (8 if kind else 16))
            if key and value:
                try:
                    name = self.object(key)
                except (ValueError, UnicodeError, struct.error):
                    if wanted is None:
                        raise
                    # Unrelated globals can reference pages outside the RAM
                    # mapping. All requested fields must still be decoded and
                    # the complete live ownership graph is validated below.
                    continue
                if wanted is None or name in wanted:
                    result[name] = value
                    positions[name] = index
                if wanted is not None and wanted <= result.keys():
                    break
        if wanted is not None and not wanted <= result.keys():
            raise ValueError('Required live dictionary fields are unavailable')
        if wanted is not None:
            cache[cache_key] = positions
        return result

    def object(self, address, depth=0):
        if depth > 6:
            raise ValueError('Python object nesting exceeds limit')
        kind = self.typename(address)
        if kind == 'str':
            header = self.read(address, 48)
            size = struct.unpack_from('<q', header, 16)[0]
            state = struct.unpack_from('<I', header, 32)[0]
            width = (state >> 2) & 7
            if not 0 <= size <= 4096 or width not in (1, 2, 4) or not state & 32:
                raise ValueError('Unsupported Python string layout')
            offset = 48 if state & 64 else 72
            return self.read(address + offset, size * width).decode({1: 'latin1', 2: 'utf-16-le', 4: 'utf-32-le'}[width])
        if kind in ('int', 'bool'):
            size = struct.unpack('<q', self.read(address + 16, 8))[0]
            if abs(size) > 8:
                raise ValueError('Python integer exceeds limit')
            digits = struct.unpack('<' + 'I' * abs(size), self.read(address + 24, abs(size) * 4))
            result = sum(digit << (30 * index) for index, digit in enumerate(digits))
            return result if size >= 0 else -result
        if kind == 'float':
            return struct.unpack('<d', self.read(address + 16, 8))[0]
        if kind == 'NoneType':
            return None
        if kind == 'bytes':
            size = self.pointer(address + 16)
            if size > 4096:
                raise ValueError('Python byte string exceeds limit')
            return self.read(address + 32, size)
        if kind == 'ObjectId':
            raw = self.object(self.pointer(address + 16), depth + 1)
            if not isinstance(raw, bytes) or len(raw) != 12:
                raise ValueError('Invalid role ObjectId')
            return raw.hex()
        if kind == 'dict':
            entries = self.dictionary(address)
            if len(entries) > 256:
                raise ValueError('Python value dictionary exceeds limit')
            return {key: self.object(value, depth + 1) for key, value in entries.items()}
        raise ValueError('Unsupported Python value type')

    def player_context(self, manager_type, *, include_currency=False):
        if self.typename(manager_type) != 'type' or self.read(self.pointer(manager_type + 24), len(self.manager_name)) != self.manager_name:
            raise ValueError('Drawing manager type changed')
        class_fields = self.dictionary(self.pointer(manager_type + 264), wanted=('__init__',))
        initializer = class_fields['__init__']
        if self.typename(initializer) != 'function':
            raise ValueError('Drawing initializer changed')
        globals_dict = self.pointer(initializer + 16)
        globals_fields = self.dictionary(globals_dict, wanted=('HuiJuanInfoMgr', 'm_instance', 'Globals'))
        if globals_fields.get('HuiJuanInfoMgr') != manager_type:
            raise ValueError('Drawing class is no longer active')
        manager = globals_fields['m_instance']
        if self.typename(manager) != 'HuiJuanInfoMgr':
            raise ValueError('Drawing singleton is unavailable')
        game_globals = globals_fields['Globals']
        if self.typename(game_globals) != 'module':
            raise ValueError('Game globals layout changed')
        player = self.dictionary(self.pointer(game_globals + 16), wanted=('player1',))['player1']
        if self.typename(player) != 'ClientAvatar':
            raise ValueError('Current player is unavailable')
        names = ('id', '_name', 'currency') if include_currency else ('id', '_name')
        player_fields = self.dictionary(self.pointer(player - 24), wanted=names)
        return manager, globals_dict, game_globals, player, player_fields

    def live_identity(self, manager_type):
        manager, globals_dict, game_globals, player, fields = self.player_context(manager_type)
        identity = {'role_id': self.object(fields['id']), 'name': self.object(fields['_name'])}
        self.data_pages.clear()
        self.table_pages.clear()
        manager2, _, _, player2, current = self.player_context(manager_type)
        if manager2 != manager or player2 != player or self.object(current['id']) != identity['role_id']:
            raise ValueError('Live identity changed during sampling')
        return identity

    def snapshot(self, manager_type, role_id, server_id, *, fragment_ids=None):
        manager, globals_dict, game_globals, player, player_fields = self.player_context(manager_type, include_currency=True)
        if self.object(player_fields['id']) != role_id:
            name = self.object(player_fields['_name']) if '_name' in player_fields else ''
            raise LiveRoleMismatch('Drawing cache belongs to another role', name)
        # CPython managed instances use a materialized dict at -24, or split
        # values at -32 with the heap type's shared keys at +880.
        fields = self.instance_fields(manager, manager_type)
        result = {'server_info': self.object(fields['serverInfo']),
                  'self_info': self.object(fields['selfInfo']),
                  'total_score': self.object(fields['unlockDonation'])}
        progress_rows(result)
        identity = {'role_id': role_id, 'server_id': server_id, 'name': self.object(player_fields['_name'])}
        def currency_counts(address):
            if fragment_ids is None:
                return self.object(address)
            if hasattr(self, 'currency_values'):
                return self.currency_values(address, fragment_ids)
            wanted = {str(value) for value in fragment_ids.values()}
            return {key: self.object(value) for key, value in self.dictionary(address).items()
                    if str(key) in wanted}

        # A long-lived account may have thousands of currencies. Only the
        # current issue's three fragment IDs are needed by this query.
        currency = currency_counts(player_fields['currency'])
        # Re-read ownership and values so a role switch or an in-place update
        # during the traversal cannot silently produce a mixed snapshot.
        self.data_pages.clear()
        self.table_pages.clear()
        if (self.dictionary(globals_dict, wanted=('m_instance',)).get('m_instance') != manager
                or self.dictionary(self.pointer(game_globals + 16), wanted=('player1',)).get('player1') != player):
            raise ValueError('Drawing singleton changed during sampling')
        current_player = self.dictionary(self.pointer(player - 24), wanted=('id', 'currency'))
        current_fields = self.instance_fields(manager, manager_type)
        if (self.object(current_player['id']) != role_id
                or self.object(current_fields['serverInfo']) != result['server_info']
                or self.object(current_fields['unlockDonation']) != result['total_score']
                or currency_counts(current_player['currency']) != currency):
            raise ValueError('Drawing data changed during sampling')
        result['_identity'], result['_currency'] = identity, currency
        result['_rankings'] = self.rank_snapshot(manager, manager_type, current_fields)
        if result['_rankings']:
            current_identity = self.live_identity(manager_type)
            if current_identity['role_id'] != role_id:
                raise LiveRoleMismatch('Drawing cache belongs to another role')
        return result

    def rank_snapshot(self, manager, manager_type, fields):
        required = ('rankInfo', 'oldRankListParam', 'refreshRankTime')
        if not all(key in fields for key in required):
            return {}
        try:
            service = self.object(fields['oldRankListParam'])
            captured = self.object(fields['refreshRankTime'])
            if (not isinstance(service, str) or not service.startswith('pic_') or not service[4:].isdigit()
                    or type(captured) not in (int, float) or not math.isfinite(captured)
                    or not 0 < captured <= time.time() + 300):
                return {}
            address = fields['rankInfo']
            if self.typename(address) != 'list' or self.pointer(address + 16) != 100:
                return {}
            items = self.pointer(address + 24)
            def cutoffs():
                scores = []
                for index in (0, 19, 99):
                    entry = self.dictionary(self.pointer(items + index * 8))
                    if self.object(entry['rank']) != index + 1:
                        raise ValueError('Rank cache does not contain the first page')
                    score = self.object(entry['score'])
                    if type(score) not in (int, float) or not math.isfinite(score) or score < 0:
                        raise ValueError('Invalid cached ranking score')
                    scores.append(score)
                if scores != sorted(scores, reverse=True):
                    raise ValueError('Invalid cached ranking order')
                return scores
            scores = cutoffs()
            self.data_pages.clear()
            self.table_pages.clear()
            current = self.instance_fields(manager, manager_type)
            if (current.get('rankInfo') != address or self.pointer(address + 24) != items
                    or self.object(current['oldRankListParam']) != service
                    or self.object(current['refreshRankTime']) != captured or cutoffs() != scores):
                return {}
            return {service[4:]: {'rank_20_score': scores[1], 'rank_100_score': scores[2],
                    'rankings': [], 'rank_source': 'game_cache',
                    'rank_queried_at': datetime.fromtimestamp(captured, timezone.utc).isoformat(timespec='seconds')}}
        except (ValueError, KeyError, TypeError, UnicodeError, struct.error, OverflowError):
            return {}

    def instance_fields(self, instance, instance_type):
        instance_dict = self.pointer(instance - 24)
        if instance_dict:
            return self.dictionary(instance_dict)
        return self.entries(self.pointer(instance_type + 880), self.pointer(instance - 32))


def read_live_snapshot(memory, anchors, account, deadline, cache_key=None, *, fragment_ids=None):
    """Validate cached locations or discover a unique, role-bound result."""
    if not account or not account.get('user_id'):
        return None
    def snapshot(guest, manager_type):
        if fragment_ids is None:
            return guest.snapshot(manager_type, account['user_id'], account['serverid'])
        return guest.snapshot(manager_type, account['user_id'], account['serverid'], fragment_ids=fragment_ids)

    cached = (_locations.get(cache_key) or _load_location(cache_key, memory)) if cache_key is not None else None
    if cached:
        region, root, manager_type = cached
        guest = GuestObjects(memory, region, deadline)
        guest.root = root
        try:
            result = snapshot(guest, manager_type)
            _locations[cache_key] = cached
            return result
        except LiveRoleMismatch as error:
            # Only a stable, twice-read identity can prove that this is a
            # different account. A name or old SDK history is insufficient.
            try:
                identity = guest.live_identity(manager_type)
            except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
                raise error
            if isinstance(identity, dict) and identity.get('role_id') != account['user_id']:
                raise LiveRoleMismatch(str(error), identity.get('name', ''), identity.get('role_id', '')) from error
            raise error
        except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
            # A temporarily empty drawing manager or changing currency is not
            # a reason to scan gigabytes again. Validate the class separately.
            try:
                identity = guest.live_identity(manager_type)
            except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
                _forget_location(cache_key)
            else:
                if identity.get('role_id') != account['user_id']:
                    raise LiveRoleMismatch('Drawing cache belongs to another role', identity.get('name', ''), identity.get('role_id', ''))
                try:
                    result = snapshot(guest, manager_type)
                    _locations[cache_key] = cached
                    return result
                except LiveRoleMismatch:
                    raise
                except (ValueError, KeyError, TypeError, UnicodeError, struct.error) as error:
                    raise LiveCacheUnavailable('Live drawing data is not ready') from error
    results = []
    locations = []
    guests = {}
    searches = {}
    role_mismatch = False
    identified = []
    for region, address in anchors:
        region = tuple(region)
        if region not in guests:
            guests[region] = GuestObjects(memory, region, deadline)
        guest = guests[region]
        for root, name_address in guest.roots(address):
            guest.root = root
            guest.data_pages.clear()
            guest.table_pages.clear()
            try:
                if guest.read(name_address, len(guest.manager_name)) != guest.manager_name:
                    continue
                key = (region, root, name_address)
                if key not in searches:
                    mapping = guest.page_mapping()
                    needle = struct.pack('<Q', name_address)
                    searches[key] = mapping, guest.scan([needle])[needle]
                mapping, matches = searches[key]
                for physical in matches:
                    if physical % 8:
                        continue
                    for page in mapping.get(physical & ~4095, []):
                        candidate = page + (physical & 4095) - 24
                        try:
                            results.append(snapshot(guest, candidate))
                            locations.append((region, root, candidate))
                        except LiveRoleMismatch:
                            role_mismatch = True
                            try:
                                identified.append(((region, root, candidate), guest.live_identity(candidate)))
                            except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
                                pass
                        except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
                            try:
                                identified.append(((region, root, candidate), guest.live_identity(candidate)))
                            except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
                                pass
            except (ValueError, UnicodeError, struct.error):
                continue
    if results and all(result == results[0] for result in results):
        if cache_key is not None:
            # One active game runtime per host process is sufficient here.
            for previous in list(_locations):
                if previous[0] == cache_key[0]:
                    _locations.pop(previous, None)
            _locations[cache_key] = locations[0]
            _save_location(cache_key, locations[0])
        return results[0]
    if not results and identified and all(item[1]['role_id'] == identified[0][1]['role_id'] for item in identified):
        if cache_key is not None:
            _locations[cache_key] = identified[0][0]
            _save_location(cache_key, identified[0][0])
        if identified[0][1]['role_id'] == account['user_id']:
            raise LiveCacheUnavailable('Live drawing data is not ready')
        raise LiveRoleMismatch('Drawing cache belongs to another role', identified[0][1].get('name', ''), identified[0][1]['role_id'])
    if role_mismatch and not results:
        raise LiveRoleMismatch('Drawing cache belongs to another role')
    return None
