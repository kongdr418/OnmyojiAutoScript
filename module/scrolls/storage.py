"""Per-device query leases and Windows-user encrypted authorization."""
import ctypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid

from filelock import FileLock


ROOT = Path(__file__).resolve().parents[2] / 'config' / 'scrolls'


def storage_key(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def binding(config):
    device = config.script.device
    values = [str(device.serial).replace('localhost:', '127.0.0.1:'),
              device.package_name.value, device.emulatorinfo_name, device.emulatorinfo_path]
    return storage_key('\n'.join(values))


def device_lock(serial):
    normalized = str(serial).replace('localhost:', '127.0.0.1:')
    ROOT.mkdir(parents=True, exist_ok=True)
    return FileLock(str(ROOT / (storage_key(normalized) + '.device.lock')))


def account_lock(account):
    ROOT.mkdir(parents=True, exist_ok=True)
    return FileLock(str(ROOT / (storage_key(account['uid']) + '.account.lock')))


def selection_lock(config_name):
    ROOT.mkdir(parents=True, exist_ok=True)
    return FileLock(str(ROOT / (storage_key(config_name) + '.selection.lock')))


class CredentialCatalog:
    """Encrypted imports scoped to OAS configurations, indexed by opaque IDs."""
    def lock(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        return FileLock(str(ROOT / 'accounts.lock'))

    def read(self):
        path = ROOT / 'accounts.dpapi'
        if not path.exists():
            return {}
        if path.stat().st_size > 8 * 1024**2:
            raise ValueError('本地账号列表过大')
        records = json.loads(protect(path.read_bytes(), decrypt=True))
        if not isinstance(records, dict) or any(
                not isinstance(record, dict) or not isinstance(record.get('account'), dict)
                or not record['account'].get('uid') for record in records.values()):
            raise ValueError('本地账号列表损坏，请重新读取')
        return records

    def replace_source(self, source, source_binding, instance, serial, records):
        with self.lock().acquire(timeout=5):
            try:
                saved = self.read()
            except (RuntimeError, ValueError):
                saved = {}
            previous_ids = {(value.get('binding'), value['account'].get('full_uid'),
                             value['account'].get('serverid'), value['account'].get('user_id')): identifier
                            for identifier, value in saved.items() if value.get('source_script') == source}
            saved = {key: value for key, value in saved.items() if value.get('source_script') != source}
            for record in records:
                account = record['account']
                previous_id = previous_ids.get((source_binding, account['full_uid'],
                                                account['serverid'], account['user_id']))
                identifier = previous_id or storage_key('\n'.join([source, source_binding, account['full_uid'],
                                                                   account['serverid'], account['user_id']]))
                saved[identifier] = {**record, 'source_script': source, 'binding': source_binding,
                                     'instance': instance, 'serial': serial}
            atomic_write(ROOT / 'accounts.dpapi', protect(json.dumps(saved, ensure_ascii=False).encode('utf-8')))


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('本地授权加密存储目前支持 Windows 上运行的 OAS')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    name = 'CryptUnprotectData' if decrypt else 'CryptProtectData'
    operation = getattr(crypt, name)
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('本机授权加密存储失败，请重新获取授权')
    kernel = ctypes.WinDLL('kernel32')
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class ScrollStore:
    def __init__(self, config_name):
        self.path = ROOT / storage_key(config_name)

    def read(self):
        path = self.path.with_suffix('.dpapi')
        if not path.exists():
            return None
        raw = path.read_bytes()
        if len(raw) > 256 * 1024:
            raise ValueError('本地绘卷授权文件损坏')
        record = json.loads(protect(raw, decrypt=True))
        if not isinstance(record, dict) or not isinstance(record.get('account'), dict) or not record['account'].get('uid'):
            raise ValueError('本地绘卷授权格式无效，请重新获取')
        return record

    def save(self, record):
        raw = json.dumps(record, ensure_ascii=False).encode('utf-8')
        atomic_write(self.path.with_suffix('.dpapi'), protect(raw))

    def result(self):
        path = self.path.with_suffix('.json')
        try:
            return json.loads(path.read_text('utf-8')) if path.exists() else None
        except (ValueError, OSError):
            return None

    def save_result(self, result):
        atomic_write(self.path.with_suffix('.json'), json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def clear_result(self):
        self.path.with_suffix('.json').unlink(missing_ok=True)

    def selection(self):
        path = self.path.with_suffix('.selection.json')
        if not path.exists():
            return ''
        try:
            value = json.loads(path.read_text('utf-8'))
            identifier = value['credential_id']
            if not isinstance(identifier, str) or (identifier and
                    (len(identifier) != 64 or any(char not in '0123456789abcdef' for char in identifier))):
                raise ValueError('invalid selection')
            return identifier
        except (KeyError, ValueError, AttributeError) as error:
            raise ValueError('查询账号选择记录损坏，请重新选择') from error

    def select(self, identifier):
        atomic_write(self.path.with_suffix('.selection.json'), json.dumps({
            'credential_id': identifier,
        }).encode('utf-8'))


@contextmanager
def game_start_lease(config, serial):
    with device_lock(serial).acquire(timeout=65):
        try:
            config_name = getattr(config, 'config_name', '')
            record = ScrollStore(config_name).read() if config_name else None
        except (RuntimeError, ValueError, OSError):
            record = None
        if record and record.get('binding') == binding(config):
            with account_lock(record['account']).acquire(timeout=65):
                yield
        else:
            yield
