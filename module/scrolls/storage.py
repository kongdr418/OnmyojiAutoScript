"""Per-device query leases and platform-appropriate encrypted authorization."""
import ctypes
from contextlib import contextmanager
import hashlib
import json
import os
import stat
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


_PORTABLE_HEADER = b'OASKEY1\x00'


def _portable_key(create=False):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ROOT / '.credential-key'
    with FileLock(str(ROOT / '.credential-key.lock')).acquire(timeout=5):
        if create and not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError as error:
            raise ValueError('本地凭证密钥缺失，请恢复 config/scrolls 中的密钥或重新读取账号') from error
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 32
                    or (os.name == 'posix' and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077))):
                raise ValueError('本地凭证密钥无效或权限不安全，应由运行 OAS 的用户持有且权限为 600')
            return stream.read(33)


def _protect_portable(data, decrypt=False):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if decrypt:
        if not data.startswith(_PORTABLE_HEADER) or len(data) < len(_PORTABLE_HEADER) + 12 + 16:
            raise ValueError('凭证格式不适用于当前平台，请重新读取本地账号')
        nonce = data[len(_PORTABLE_HEADER):len(_PORTABLE_HEADER) + 12]
        try:
            return AESGCM(_portable_key()).decrypt(nonce, data[len(_PORTABLE_HEADER) + 12:], _PORTABLE_HEADER)
        except InvalidTag as error:
            raise ValueError('本地凭证校验失败，请确认密钥与凭证配套或重新读取账号') from error
    nonce = os.urandom(12)
    return _PORTABLE_HEADER + nonce + AESGCM(_portable_key(create=True)).encrypt(nonce, data, _PORTABLE_HEADER)


def protect(data, decrypt=False):
    if os.name == 'posix':
        return _protect_portable(data, decrypt)
    if os.name != 'nt':
        raise RuntimeError('当前系统不支持本地凭证加密存储')
    if decrypt and data.startswith(_PORTABLE_HEADER):
        raise ValueError('凭证属于 Linux 环境，请在当前系统重新读取本地账号')
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
