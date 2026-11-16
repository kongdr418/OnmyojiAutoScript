"""Recover saved official-channel SDK sessions and role history from disk."""
import base64
import hashlib
import hmac
import io
import json
import re
import struct
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from module.scrolls.disk import Vdi, userdata
from module.scrolls.mumu import normalize_account


MAP_HEADER = bytes.fromhex(
    'aced0005737200116a6176612e7574696c2e486173684d61700507dac1c31660d1'
    '03000246000a6c6f6164466163746f724900097468726573686f6c647870')
LIST_HEADER = bytes.fromhex(
    'aced0005737200136a6176612e7574696c2e41727261794c6973747881d21d99c7619d'
    '03000149000473697a657870')
ARRAY_HEADER = bytes.fromhex('7200025b42acf317f8060854e00200007870')


class JavaReader:
    """Only the SDK's string maps and byte-array lists, without object loading."""
    def __init__(self, data):
        if len(data) > 8 * 1024**2:
            raise ValueError('SDK 本地记录过大')
        self.stream = io.BytesIO(data)
        self.references = {}
        self.handle = 0x7e0002

    def read(self, size):
        value = self.stream.read(size)
        if len(value) != size:
            raise ValueError('SDK 本地记录不完整')
        return value

    def expect(self, value):
        if self.read(len(value)) != value:
            raise ValueError('不支持的 SDK 本地记录格式')

    def integer(self, size=4):
        return int.from_bytes(self.read(size), 'big')

    def remember(self, value):
        self.references[self.handle] = value
        self.handle += 1
        return value

    def reference(self):
        reference = self.integer()
        if reference not in self.references:
            raise ValueError('SDK 本地记录引用无效')
        return self.references[reference]

    def string(self):
        kind = self.read(1)
        if kind == b'\x70':
            return None
        if kind == b'\x71':
            return self.reference()
        if kind != b'\x74':
            raise ValueError('SDK 本地记录包含不支持的数据')
        size = self.integer(2)
        return self.remember(self.read(size).decode('utf-8'))

    def finish(self):
        if self.stream.read() != b'x':
            raise ValueError('SDK 本地记录尾部无效')


def java_map(data):
    reader = JavaReader(data)
    reader.expect(MAP_HEADER)
    reader.read(8)  # load factor and threshold
    reader.expect(b'w\x08')
    reader.integer()  # capacity
    count = reader.integer()
    if count > 2048:
        raise ValueError('SDK 记录数量超出范围')
    result = {}
    for _ in range(count):
        key, value = reader.string(), reader.string()
        if key is None or key in result:
            raise ValueError('SDK 记录键无效')
        result[key] = value
    reader.finish()
    return result


def java_list(data):
    reader = JavaReader(data)
    reader.expect(LIST_HEADER)
    count = reader.integer()
    reader.expect(b'w\x04')
    if reader.integer() != count or count > 256:
        raise ValueError('SDK 账号数量超出范围')
    values = []
    for _ in range(count):
        reader.expect(b'\x75')
        descriptor = reader.read(1)
        if descriptor == b'\x72':
            reader.expect(ARRAY_HEADER[1:])
            reader.remember('[B')
        elif descriptor == b'\x71' and reader.reference() == '[B':
            pass
        else:
            raise ValueError('SDK 账号数组格式无效')
        size = reader.integer()
        if size > 1024**2:
            raise ValueError('SDK 账号记录过大')
        values.append(reader.remember(reader.read(size)))
    reader.finish()
    return values


def decode_store(prefs, field):
    raw = java_map(bytes.fromhex(prefs['raw']))
    encoded = base64.b64decode(b''.join(bytes.fromhex(raw['1']).split()), validate=True).decode('ascii')
    key = bytearray(bytes.fromhex(encoded))
    previous = 127
    for index, value in enumerate(key):
        previous = (previous ^ (value * 0x3a0b567)) & 255
        key[index] = previous
    # The SDK's channel-string transformation produces a zero IV.
    decoder = Cipher(algorithms.AES(bytes(key)), modes.CBC(bytes(16))).decryptor()
    payload = decoder.update(bytes.fromhex(prefs[field])) + decoder.finalize()
    unpadder = PKCS7(128).unpadder()
    payload = unpadder.update(payload) + unpadder.finalize()
    if len(payload) < 32 or not hmac.compare_digest(hashlib.md5(payload[16:-16]).digest(), payload[-16:]):
        raise ValueError('SDK 本地记录校验失败，请重新读取')
    return java_map(payload[16:-16])


def client_config(raw):
    values = json.loads(bytes.fromhex(raw.decode('ascii')))
    result = {}
    for key, value in values.items():
        try:
            key = base64.b64decode(key).decode('utf-8')
        except (ValueError, UnicodeError):
            pass
        result[key] = value
    return result


def candidates(config, sessions, device):
    history = config.get('local_all_login_record', {})
    if not isinstance(history, dict):
        raise ValueError('本地角色历史格式无效')
    records = []
    for username, servers in history.items():
        match = re.fullmatch(r'([^@]+)@(ad|ios)\.netease\.win\.163\.com', username)
        if not match or not isinstance(servers, dict):
            continue
        uid, platform = match.groups()
        session = sessions.get(uid)
        if not session or not device.get('12') or not device.get('1') or not config.get('last_patch_version'):
            continue
        for server_id, roles in servers.items():
            if not str(server_id).isdigit() or not isinstance(roles, list):
                continue
            for role in roles:
                if (not isinstance(role, dict) or role.get('deactive') or
                        not re.fullmatch(r'[0-9a-f]{24}', str(role.get('_id', ''))) or
                        str(role.get('server_id', server_id)) != str(server_id)):
                    continue
                params = {'gameid': 'g37', 'username': username, 'sessionid': session,
                          'udid': device['12'], 'deviceid': device['1'], 'sdk_version': '5.18.0'}
                account = normalize_account({
                    'serverid': str(server_id), 'full_uid': username, 'session': session,
                    'sauth_str': urlencode(params), 'game': 'g37', 'login_channel': 'netease',
                    'platform': platform, 'app_channel': device.get('9', ''),
                    'patch_version': config['last_patch_version'], 'client_cloud': True,
                    'user_id': role['_id'],
                })
                records.append({'account': account, 'identity': {
                    'role_id': role['_id'], 'name': str(role.get('player_name') or role.get('player_id') or role['_id']),
                    'server_id': str(server_id), 'server_name': str(role.get('server_name', '')),
                }})
                if len(records) > 512:
                    raise ValueError('本地角色数量超出范围')
    return records


def parse_accounts(prefs_raw, config_raw):
    try:
        prefs = {node.attrib.get('name'): node.text or '' for node in ET.fromstring(prefs_raw)}
    except ET.ParseError as error:
        raise ValueError('游戏本地账号记录正在更新，请稍后重试') from error
    if not all(prefs.get(field) for field in ('raw', 'data', 'dev')):
        return None
    try:
        store = decode_store(prefs, 'data')
        sessions = {}
        for item in java_list(bytes.fromhex(store.get('0') or '')):
            record = java_map(item)
            if record.get('1'):
                sessions[record['1']] = record.get('2')
        return candidates(client_config(config_raw), sessions, decode_store(prefs, 'dev'))
    except (KeyError, ValueError, UnicodeError, struct.error) as error:
        raise ValueError('SDK 本地记录无法解析，请重新读取或更新游戏授权') from error


def read_redroid_accounts(device):
    files = device.redroid_files
    found, found_store = [], False
    for package in files.packages():
        root = f'/data/user/0/{package}/shared_prefs'
        config_path = f'/sdcard/Android/data/{package}/files/netease/onmyoji/Documents/clientconfig'
        names = files.names(root)
        config_raw = files.read(config_path)
        for name in names:
            if not re.fullmatch(r'com\.netease\.mpay\.[\w.]+\.xml', name, flags=re.ASCII):
                continue
            path = root + '/' + name
            prefs_raw = files.read(path)
            records = parse_accounts(prefs_raw, config_raw)
            if records is None:
                continue
            if files.read(path) != prefs_raw or files.read(config_path) != config_raw:
                raise ValueError('游戏正在更新本地账号记录，请稍后重新读取')
            found.extend(records)
            found_store = True
    if not found_store:
        raise ValueError('未找到可用的网易官服凭证，请先在该设备登录一次查询小号，再重新读取')
    return found


def read_accounts(device):
    if device.instance.type == 'redroid':
        return read_redroid_accounts(device)
    path = device.data_disk()
    with Vdi(path) as disk:
        fs = userdata(disk)
        packages = [device.package]
        if device.package == 'auto':
            packages = [name for name in fs.directory(fs.lookup('/data')) if name.startswith('com.netease.onmyoji')]
        found = []
        found_store = False
        for package in packages:
            root = f'/data/{package}/shared_prefs'
            config_path = f'/media/0/Android/data/{package}/files/netease/onmyoji/Documents/clientconfig'
            try:
                config_raw = fs.file(config_path)
                names = fs.directory(fs.lookup(root))
            except KeyError:
                continue
            for name in names:
                if not name.startswith('com.netease.mpay.') or not name.endswith('.xml'):
                    continue
                prefs_raw = fs.file(root + '/' + name)
                records = parse_accounts(prefs_raw, config_raw)
                if records is None:
                    continue
                found.extend(records)
                found_store = True
                if fs.file(root + '/' + name) != prefs_raw or fs.file(config_path) != config_raw:
                    raise ValueError('游戏正在更新本地账号记录，请稍后重新读取')
    if not found_store:
        raise ValueError('未找到可用的网易官服凭证，请先在该模拟器登录一次查询小号，再重新读取')
    return found
