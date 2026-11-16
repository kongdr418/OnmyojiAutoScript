"""Read a rooted redroid game's virtual memory through one read-only ADB stream."""
from bisect import bisect_right
import json
import re
import shlex
import socket
import struct
import time

from module.scrolls import storage
from module.scrolls.live_objects import GuestObjects, LiveCacheUnavailable, LiveRoleMismatch


_locations = {}
_fields = {}


def parse_maps(text):
    rows = []
    for line in text.splitlines():
        match = re.fullmatch(r'([0-9a-f]+)-([0-9a-f]+)\s+([rwxps-]{4})\s+([0-9a-f]+)\s+\S+\s+\d+(?:\s+(.*))?', line)
        if not match:
            raise ValueError('游戏内存映射格式无效')
        start, end = int(match[1], 16), int(match[2], 16)
        if match[3][0] == 'r' and 0 < start < end <= 1 << 47:
            rows.append((start, end, match[3], match[5] or ''))
    rows.sort()
    if not rows or len(rows) > 30000 or any(a[1] > b[0] for a, b in zip(rows, rows[1:])):
        raise ValueError('游戏内存映射不可用')
    return rows


class ProcessMemory:
    def __init__(self, files, runtime, deadline):
        self.files, self.runtime, self.deadline = files, runtime, deadline
        self.stream = None
        if len(runtime) != 1 or type(runtime[0][0]) is not int or runtime[0][0] <= 0:
            raise ValueError('redroid 游戏进程信息无效')
        self.pid = runtime[0][0]
        self.maps = parse_maps(files.read(f'/proc/{self.pid}/maps').decode('utf-8'))
        self.starts = [r[0] for r in self.maps]
        boot = files.read('/proc/sys/kernel/random/boot_id').decode('ascii').strip()
        if not re.fullmatch(r'[0-9a-f-]{36}', boot):
            raise ValueError('redroid 启动标识无效')
        self.key = (files.device.serial, boot, runtime)

    def check_time(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('读取 redroid 绘卷内存超时')
        return min(remaining, 10)

    def region(self, address, size):
        index = bisect_right(self.starts, address) - 1
        if index < 0 or size < 0 or address + size > self.maps[index][1]:
            raise ValueError('游戏内存地址已失效')
        return self.maps[index]

    def _open(self):
        # Select the configured ADB transport. No adb-root/restart, forwarded
        # port, helper file, injected code, or writes to the game process.
        self.files.root_command(['true'])
        client = self.files.device.adb_client
        connect = getattr(client, 'make_connection', None) or client._connect
        stream = connect(timeout=self.check_time())
        self.stream = stream
        stream.send_command('host:transport:' + self.files.device.serial)
        stream.check_okay()
        script = ('while read -r offset count; do '
                  f'dd if=/proc/{self.pid}/mem bs=65536 iflag=skip_bytes,count_bytes '
                  'skip="$offset" count="$count" status=none 2>/dev/null '
                  '|| exit 1; done')
        stream.send_command('exec:' + shlex.join(self.files.root + ['sh', '-c', script]))
        stream.check_okay()
        stream.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _receive(self, size):
        result = bytearray()
        while len(result) < size:
            self.stream.conn.settimeout(self.check_time())
            if hasattr(socket, 'TCP_QUICKACK'):
                try:
                    self.stream.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
                except OSError:
                    pass
            data = self.stream.conn.recv(size - len(result))
            if not data:
                raise OSError('redroid 内存只读连接已关闭')
            result.extend(data)
        return bytes(result)

    def read(self, address, size):
        self.region(address, size)
        if not 0 < size <= 4 * 1024**2:
            raise ValueError('redroid 内存读取长度无效')
        if self.stream is None:
            self._open()
        try:
            self.stream.conn.settimeout(self.check_time())
            self.stream.conn.sendall(f'{address} {size}\n'.encode('ascii'))
            # dd either returns exactly this many bytes or terminates the
            # stream. The next request is sent only after this read completes.
            return self._receive(size)
        except BaseException:
            self.close()
            raise

    def scan_regions(self):
        rows = [r for r in self.maps if r[2] == 'rw-p'
                and (not r[3] or r[3] == '[heap]' or 'scudo' in r[3])]
        # Native CPython objects live in primary allocator slabs; large
        # secondary allocations and anonymous asset buffers come afterwards.
        return sorted(rows, key=lambda r: (0 if 'scudo:primary' in r[3] else 1 if not r[3] else 2,
                                          -(r[1] - r[0])))

    def find(self, needles):
        overlap = max(map(len, needles)) - 1
        for start, end, _, _ in self.scan_regions():
            tail = b''
            for address in range(start, end, 4 * 1024**2):
                data = tail + self.read(address, min(4 * 1024**2, end - address))
                for needle in needles:
                    cursor = 0
                    while True:
                        index = data.find(needle, cursor)
                        if index < 0:
                            break
                        cursor = index + 1
                        yield needle, address - len(tail) + index
                tail = data[-overlap:]

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class ProcessObjects(GuestObjects):
    def __init__(self, memory):
        super().__init__(memory, (0, 0), memory.deadline)
        self.field_cache = _fields.setdefault(memory.key, {})

    def currency_values(self, address, item_ids):
        """Look up typed integer currencies using CPython's hash table."""
        if self.typename(address) != 'dict':
            raise ValueError('Current currency dictionary is unavailable')
        keys, values = self.pointer(address + 32), self.pointer(address + 40)
        header = self.read(keys, 32)
        logsize, logbytes, kind = struct.unpack_from('<BBB', header, 8)
        count = struct.unpack_from('<q', header, 24)[0]
        if (not 0 <= count <= 8192 or not 0 <= logsize <= 15 or not logsize <= logbytes <= logsize + 3
                or kind not in (0, 1, 2)):
            raise ValueError('Currency dictionary layout changed')
        if kind:
            wanted = {str(item) for item in item_ids.values()}
            return {k: self.object(v) for k, v in self.dictionary(address).items() if str(k) in wanted}
        entries, width, mask = keys + 32 + (1 << logbytes), 1 << (logbytes - logsize), (1 << logsize) - 1
        result = {}
        for wanted in item_ids.values():
            if type(wanted) is not int or not 0 <= wanted < 1 << 30:
                raise ValueError('Current currency id is invalid')
            index = wanted & mask
            perturb = wanted
            for _ in range((1 << logsize) + 1):
                slot = int.from_bytes(self.read(keys + 32 + index * width, width), 'little', signed=True)
                if slot == -1:
                    break
                if slot >= 0:
                    if slot >= count:
                        raise ValueError('Currency hash entry is invalid')
                    entry = entries + slot * 24
                    stored_hash = struct.unpack('<q', self.read(entry, 8))[0]
                    if stored_hash == wanted and self.object(self.pointer(entry + 8)) == wanted:
                        value = self.pointer(values + slot * 8) if values else self.pointer(entry + 16)
                        if value:
                            result[wanted] = self.object(value)
                            break
                elif slot != -2:
                    raise ValueError('Currency hash index is invalid')
                perturb >>= 5
                index = (index * 5 + perturb + 1) & mask
            else:
                raise ValueError('Currency hash lookup exceeded its limit')
        return result

    def read(self, address, size):
        if not 0 < address < 1 << 47 or not 0 <= size <= 65536:
            raise ValueError('游戏对象地址或长度无效')
        result = bytearray()
        while size:
            self.check_time()
            start, end, _, _ = self.memory.region(address, 1)
            page = address & ~4095
            if page not in self.data_pages:
                block = max(start, address & ~65535)
                data = self.memory.read(block, min(65536, end - block))
                for offset in range(0, len(data), 4096):
                    self.data_pages[block + offset] = data[offset:offset + 4096]
            offset = address - page
            take = min(size, len(self.data_pages[page]) - offset)
            if take <= 0:
                raise ValueError('游戏内存页面已变化')
            result.extend(self.data_pages[page][offset:offset + take])
            address, size = address + take, size - take
        return bytes(result)


def _path(key):
    return storage.ROOT / 'process-locations' / (storage.storage_key(json.dumps(key)) + '.json')


def read_redroid_memory(files, runtime, account, *, fragment_ids, cached_only=False, timeout=55,
                        persist_locations=True):
    if not account.get('user_id') or not fragment_ids:
        raise ValueError('所选角色或当前活动信息不完整')
    memory = ProcessMemory(files, runtime, time.monotonic() + timeout)
    guest = ProcessObjects(memory)
    try:
        manager_type = _locations.get(memory.key)
        if manager_type is None and persist_locations:
            try:
                path = _path(memory.key)
                if path.stat().st_size <= 128:
                    manager_type = json.loads(path.read_text('utf-8'))['type']
            except (OSError, ValueError, KeyError, TypeError):
                pass

        def sample(candidate):
            _locations[memory.key] = candidate  # Hint only; every use validates the live graph.
            identity = guest.live_identity(candidate)
            _locations[memory.key] = candidate
            if persist_locations:
                try:
                    storage.atomic_write(_path(memory.key), json.dumps({'type': candidate}).encode())
                except OSError:
                    pass
            if identity['role_id'] != account['user_id']:
                raise LiveRoleMismatch('Drawing cache belongs to another role', identity['name'], identity['role_id'])
            try:
                return guest.snapshot(candidate, account['user_id'], account['serverid'], fragment_ids=fragment_ids)
            except LiveRoleMismatch:
                raise
            except (ValueError, KeyError, TypeError, UnicodeError, struct.error) as error:
                guest.data_pages.clear()
                guest.table_pages.clear()
                try:
                    current = guest.live_identity(candidate)
                    if current['role_id'] != account['user_id']:
                        raise LiveRoleMismatch('Drawing cache belongs to another role', current['name'], current['role_id'])
                    return guest.snapshot(candidate, account['user_id'], account['serverid'], fragment_ids=fragment_ids)
                except LiveRoleMismatch:
                    raise
                except (ValueError, KeyError, TypeError, UnicodeError, struct.error) as retry:
                    raise LiveCacheUnavailable('Live drawing data is not ready') from retry

        if type(manager_type) is int:
            try:
                return dict(account), sample(manager_type)
            except (LiveRoleMismatch, LiveCacheUnavailable):
                raise
            except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
                _locations.pop(memory.key, None)
                guest.data_pages.clear()
        if cached_only:
            return None, None
        tried = set()
        for _, name in memory.find([guest.manager_name]):
            if name in tried:
                continue
            tried.add(name)
            for _, reference in memory.find([struct.pack('<Q', name)]):
                candidate = reference - 24
                if candidate % 8:
                    continue
                guest.data_pages.clear()
                try:
                    return dict(account), sample(candidate)
                except (LiveRoleMismatch, LiveCacheUnavailable):
                    raise
                except (ValueError, KeyError, TypeError, UnicodeError, struct.error):
                    continue
        return None, None
    finally:
        memory.close()
