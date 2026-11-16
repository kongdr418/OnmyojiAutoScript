"""Read-only MuMu host RAM scanning, without VM debugger or injected code."""
import ctypes
import json
import os
import struct
import time
from urllib.parse import parse_qsl

import msgpack
import psutil
from google.protobuf.message import DecodeError
from module.scrolls.client import make_message, progress_rows
from module.scrolls.codec import decode_extension



def normalize_account(record):
    result = dict(record)
    parameters = dict(parse_qsl(str(result.get('sauth_str', '')), keep_blank_values=True))
    server_id = result.get('serverid') or result.get('host_id')
    if parameters.get('gameid') != 'g37' or not server_id:
        raise ValueError('未找到完整的阴阳师授权')
    result.update(parameters)
    username = result.get('username') or result.get('full_uid')
    session = result.get('sessionid') or result.get('session')
    if not isinstance(username, str) or '@' not in username or not isinstance(session, str) or not session:
        raise ValueError('阴阳师授权不完整')
    result.update(uid=username.split('@', 1)[0], full_uid=username, username=username,
                  session=session, sessionid=session, serverid=str(server_id),
                  host_id=str(server_id), is_login_in_mumu=True)
    return result


def _response_entity(data, parameters_start, parameters_size, method_index=5):
    end = parameters_start + parameters_size
    for start in range(max(0, parameters_start - 128), parameters_start - 6):
        if data[start + 4:start + 6] != struct.pack('<H', method_index):
            continue
        if struct.unpack_from('<I', data, start)[0] != end - start - 4:
            continue
        reply = make_message('EntityMessage')
        try:
            reply.ParseFromString(data[start + 6:end])
        except DecodeError:
            continue
        if len(reply.id) == 12 and reply.parameters == data[parameters_start:end]:
            return reply.id.hex()
    return None


def _packed_maps(data, marker):
    offset = 0
    while True:
        at = data.find(marker, offset)
        if at < 0:
            return
        offset = at + len(marker)
        for before in range(1, 65):
            start = at - before
            if start < 0 or not 0x80 <= data[start] <= 0x8f:
                continue
            try:
                unpacker = msgpack.Unpacker(raw=False, strict_map_key=False,
                                           ext_hook=decode_extension, max_buffer_size=65536)
                unpacker.feed(data[start:start + 65536])
                value = next(unpacker)
                if isinstance(value, dict):
                    yield start, value, unpacker.tell()
            except (ValueError, TypeError, StopIteration, msgpack.UnpackException):
                continue


def scan_block(data):
    """Only retain SDK authorization and validated drawing response maps."""
    records, progress, selections = [], [], []
    offset = 0
    while True:
        at = data.find(b'"sauth_str"', offset)
        if at < 0:
            break
        offset = at + 11
        end = data.find(b'\0', at, min(len(data), at + 24000))
        if end < 0:
            continue
        cursor = at
        for _ in range(32):
            start = data.rfind(b'{', max(0, at - 16000), cursor)
            if start < 0:
                break
            cursor = start
            try:
                value, _ = json.JSONDecoder().raw_decode(data[start:end].decode('utf-8'))
                if isinstance(value, dict) and value.get('session'):
                    records.append(normalize_account(value))
                    break
            except (ValueError, UnicodeError):
                continue
    for _, value, _ in _packed_maps(data, b'\xacaccount_info'):
        account = value.get('account_info')
        if isinstance(account, dict):
            try:
                records.append(normalize_account(account))
            except ValueError:
                continue
    for start, value, size in _packed_maps(data, b'\xa9avatar_id'):
        role_id = value.get('avatar_id')
        if not isinstance(role_id, str) or len(role_id) != 24 or 'client_cache_data_md5' not in value:
            continue
        try:
            if len(bytes.fromhex(role_id)) != 12:
                continue
        except ValueError:
            continue
        entity = _response_entity(data, start, size, method_index=3)
        if entity:
            selections.append((entity, role_id))
    for start, value, size in _packed_maps(data, b'server_info'):
        if 'server_info' not in value:
            continue
        try:
            progress_rows(value)
        except (ValueError, KeyError, TypeError):
            continue
        entity = _response_entity(data, start, size)
        if entity:
            progress.append((entity, value))
    return records, progress, selections


def low_ram_regions(regions):
    """Recognize MuMu's split conventional/extended x86 low RAM mappings."""
    regions = set(regions)
    pairs = [base for base, size in regions
             if size == 0xa0000 and (base + size, 0x100000) in regions]
    if len(pairs) != 1:
        return []
    base = pairs[0]
    # The VGA/firmware hole between 640 KiB and 1 MiB is not guest RAM.
    return [(0, base, 0xa0000), (0x100000, base + 0xa0000, 0x100000)]


class WindowsMemory:
    def __init__(self, pid):
        if os.name != 'nt':
            raise RuntimeError('内存读取目前支持 Windows MuMu')
        from ctypes import wintypes

        class MemoryInfo(ctypes.Structure):
            _fields_ = [('base', ctypes.c_void_p), ('allocation', ctypes.c_void_p),
                        ('allocation_protect', wintypes.DWORD), ('partition', wintypes.WORD),
                        ('size', ctypes.c_size_t), ('state', wintypes.DWORD),
                        ('protect', wintypes.DWORD), ('kind', wintypes.DWORD)]

        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MemoryInfo), ctypes.c_size_t]
        self.kernel.VirtualQueryEx.restype = ctypes.c_size_t
        self.kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.ReadProcessMemory.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x410, False, pid)
        if not self.handle:
            raise RuntimeError('没有读取 MuMu 内存的权限，请使用与模拟器相同权限运行 OAS')
        self.regions = []
        self.low_regions = []
        try:
            address = 0
            small = []
            while True:
                info = MemoryInfo()
                if not self.kernel.VirtualQueryEx(self.handle, address, ctypes.byref(info), ctypes.sizeof(info)):
                    break
                base = info.base or 0
                if info.state == 0x1000 and info.kind == 0x20000 and not info.protect & 0x101 and info.size >= 512 * 1024**2:
                    self.regions.append((base, info.size))
                if (info.state == 0x1000 and info.kind == 0x20000 and not info.protect & 0x101
                        and info.protect & 0xff == 0x40
                        and info.size in (0xa0000, 0x100000)):
                    small.append((base, info.size))
                address = base + info.size
            self.low_regions = low_ram_regions(small)
            if not self.regions or sum(size for _, size in self.regions) > 12 * 1024**3:
                raise RuntimeError('MuMu 内存布局暂未适配')
        except BaseException:
            self.close()
            raise

    def read(self, address, size):
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(count)) or count.value != size:
            raise ValueError('MuMu memory range became unreadable')
        return buffer.raw

    def close(self):
        self.kernel.CloseHandle(self.handle)


def read_game_memory(pid, runtime=None, account_hint=None, *, selected_only=False, fragment_ids=None,
                     cached_only=False, timeout=55):
    from module.scrolls.live_objects import GuestObjects, read_live_snapshot
    from module.logger import logger

    if selected_only and (not runtime or not account_hint or not account_hint.get('user_id') or not fragment_ids):
        raise ValueError('所选角色或当前活动信息不完整，无法安全读取游戏内存')
    records, progress, selections, anchors = [], [], [], []
    started = time.monotonic()
    deadline = started + timeout
    process_key = (pid, psutil.Process(pid).create_time(), runtime) if runtime else None
    memory = WindowsMemory(pid)
    try:
        if process_key and account_hint and account_hint.get('user_id'):
            cache_key = process_key + (account_hint['uid'], account_hint['user_id'], account_hint.get('serverid'))
            live = read_live_snapshot(memory, [], account_hint, deadline, cache_key, fragment_ids=fragment_ids)
            if live is not None:
                logger.info(f'绘卷内存查询 直接读取已校验对象 耗时={time.monotonic() - started:.3f}s')
                return dict(account_hint), live
        if cached_only:
            return None, None
        for base, size in memory.regions:
            tail = b''
            for offset in range(0, size, 16 * 1024**2):
                if time.monotonic() > deadline:
                    raise TimeoutError('读取游戏缓存超时，请稍后重试')
                try:
                    block = tail + memory.read(base + offset, min(16 * 1024**2, size - offset))
                except ValueError:
                    tail = b''
                    continue
                if not selected_only:
                    found, snapshots, selected = scan_block(block)
                    records.extend(found)
                    progress.extend(snapshots)
                    selections.extend(selected)
                cursor = 0
                while True:
                    at = block.find(GuestObjects.manager_name, cursor)
                    if at < 0:
                        break
                    cursor = at + 1
                    physical = GuestObjects.physical_base + offset - len(tail) + at
                    anchor = ((base, size), physical)
                    if anchor not in anchors:
                        anchors.append(anchor)
                tail = block[-65536:]
        if selected_only:
            account, snapshot = account_hint, None
        else:
            account, snapshot = _select_buffers(records, progress, selections)
        scanned_at = time.monotonic()
        cache_key = None
        if account and process_key:
            cache_key = process_key + (account['uid'], account.get('user_id'), account.get('serverid'))
        live = read_live_snapshot(memory, anchors[:8], account, deadline, cache_key, fragment_ids=fragment_ids)
        finished = time.monotonic()
        scan_stage = '扫描锚点' if selected_only else '扫描授权'
        logger.info(f'绘卷内存查询 {scan_stage}={scanned_at - started:.3f}s '
                    f'定位及读取对象={finished - scanned_at:.3f}s 总耗时={finished - started:.3f}s')
        if selected_only and live is None:
            return None, None
        return account, live if live is not None else snapshot
    finally:
        memory.close()


def _select_buffers(records, progress, selections):
    def record_time(record):
        try:
            return float(record.get('timestamp', 0))
        except (ValueError, TypeError):
            return 0
    account = max(records, key=record_time) if records else None
    if account and selections:
        account['user_id'] = max(selections, key=lambda item: item[0])[1]
    def snapshot_order(value):
        rows = progress_rows(value)
        return (max(int(row['painting_id']) for row in rows), sum(row['score'] for row in rows))
    role_id = str(account.get('user_id') or '') if account else ''
    matching = [value for entity, value in progress if entity == role_id]
    snapshot = max(matching, key=snapshot_order) if matching else None
    return account, snapshot
