"""Resolve the current drawing issue from official, content-addressed resources."""
from dataclasses import dataclass
import hashlib
import json
import re
import struct
import threading
from urllib.parse import urlsplit
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import requests

from module.scrolls.bindict import BindictReader
from module.scrolls.marshal_data import read_code_data
from module.scrolls import storage


VERSION_URL = 'https://g37.update.netease.com/pl/onmyoji_astc4_trunk.txt'
# Stable resource-name hashes in the game's script3 THFB index.
RESOURCES = {'Constant': 17020542228617769339, 'Paintings': 606226905126934893}
LIMIT = 8 * 1024**2
_lock = threading.Lock()
_parsed = {}


@dataclass(frozen=True)
class Activity:
    issue: int
    version: str
    fingerprint: str
    painting_ids: tuple
    fragment_ids: dict
    fragment_points: dict

    def metadata(self):
        return {'issue': self.issue, 'version': self.version, 'fingerprint': self.fingerprint,
                'painting_ids': list(self.painting_ids), 'fragment_ids': self.fragment_ids,
                'fragment_points': self.fragment_points}

    def matches(self, result):
        return (result.get('activity', {}).get('fingerprint') == self.fingerprint
                and {row.get('painting_id') for row in result.get('scrolls', [])} == set(self.painting_ids))


def download(url, limit=LIMIT):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'g37.update.netease.com', 'g37.gsf.netease.com'}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('活动配置资源地址无效')
    with requests.get(url, timeout=(5, 10), stream=True, allow_redirects=False,
                      headers={'Cache-Control': 'no-cache'}) as response:
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError('活动配置资源地址已变化')
        data = bytearray()
        for block in response.iter_content(65536):
            data.extend(block)
            if len(data) > limit:
                raise ValueError('活动配置资源过大')
    return bytes(data)


def parse_version(data):
    text = data.decode('utf-8')
    versions = re.findall(r'^(\d+\.\d+\.\d+)\s+([A-Za-z0-9_.-]+)\s*$', text, re.M)
    base = re.search(r'^#base_url\s+(https://\S+)\s*$', text, re.M)
    if not versions or not base:
        raise ValueError('无法识别当前游戏资源版本')
    version, info = max(versions, key=lambda item: tuple(map(int, item[0].split('.'))))
    return version, info, base[1].rstrip('/') + '/'


def resource_records(data):
    """Read the two named script resources from the bounded THFB FlatBuffer."""
    if data[:4] != b'THFB':
        raise ValueError('活动资源索引格式已变化')
    def number(fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            raise ValueError('活动资源索引不完整')
        return struct.unpack_from(fmt, data, offset)[0]
    def pointer(offset):
        return offset + number('<I', offset)
    def field(table, index):
        vtable = table - number('<i', table)
        if 4 + index * 2 >= number('<H', vtable):
            raise ValueError('活动资源索引字段缺失')
        offset = number('<H', vtable + 4 + index * 2)
        if not offset:
            raise ValueError('活动资源索引字段为空')
        return table + offset
    root = pointer(8)
    pair = pointer(field(root, 1))
    keys = pointer(field(pointer(field(pair, 1)), 0))
    values = pointer(field(pair, 0))
    count = number('<I', keys)
    if (count > 200000 or count != number('<I', values) or
            keys + 4 + count * 8 > len(data) or values + 4 + count * 24 > len(data)):
        raise ValueError('活动资源索引长度无效')
    wanted = {value: key for key, value in RESOURCES.items()}
    result = {}
    for index in range(count):
        key = number('<Q', keys + 4 + index * 8)
        if key in wanted:
            offset = values + 4 + index * 24
            size = number('<I', offset + 4)
            if not 8 < size <= LIMIT:
                raise ValueError('活动配置长度无效')
            result[wanted[key]] = {'digest': data[offset + 8:offset + 24].hex(), 'size': size}
    if result.keys() != RESOURCES.keys():
        raise ValueError('官方资源中未找到当前绘卷配置')
    return result


def decode_payload(payload, digest):
    if not 8 <= len(payload) <= LIMIT:
        raise ValueError('活动配置数据长度无效')
    tag, power, salt = struct.unpack_from('<HBB', payload)
    if power > 20 or tag not in (0x4341, 0x4350, 0x4358):
        raise ValueError('活动配置加密格式已变化')
    body = bytearray(payload[8:])
    size = len(body)
    prefix = min(size, 128 << (power - 1)) if power else 0
    seed = (salt + size) & 0xff
    offset = 0
    if tag != 0x4358:
        first = 0x7c2e6b6a00000000 | ((size << 8) & 0xffff0000) | (seed << 8) | (size % 0xfd)
        second = 0x5c74656e00003630 | ((seed ^ 0x33) << 16) | ((seed | 0x2e) << 24)
        key = struct.pack('<QQ', first, second)
        offset = prefix // 16 * 16
        decoder = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        body[:offset] = decoder.update(bytes(body[:offset])) + decoder.finalize()
    remaining = prefix - offset
    mirrored = min(offset, remaining)
    for index in range(mirrored):
        body[offset + index] ^= (seed + index + body[index]) & 255
    for index in range(mirrored, remaining):
        body[offset + index] ^= (seed + index) & 255
    header = bytes(body[:64])
    body[:len(header)] = bytes(value ^ 0x5a for value in reversed(header))
    if hashlib.md5(body[4:]).hexdigest() != digest:
        raise ValueError('活动配置内容校验失败')
    if body[:4] == b'ENON':
        inflater = zlib.decompressobj()
        data = inflater.decompress(body[4:], LIMIT + 1)
        if len(data) > LIMIT or not inflater.eof or inflater.unused_data:
            raise ValueError('活动配置解压长度无效')
    else:
        raise ValueError('活动配置压缩格式已变化')
    return data


def read_table(data, name):
    code = read_code_data(data)
    filename = code.filename.replace('\\', '/')
    if filename != f'com/data/cdata/{name}.py':
        raise ValueError('活动配置资源名称不匹配')
    tables = []
    for value in code.constants:
        if isinstance(value, bytes) and len(value) >= 8:
            try:
                rows = BindictReader(value, native_containers=True).rows()
            except ValueError:
                continue
            tables.append({row['key']: row['value'] for row in rows})
    if len(tables) != 1:
        raise ValueError('活动配置数据表格式已变化')
    return tables[0]


def build_activity(version, records, tables):
    constants, paintings = tables['Constant'], tables['Paintings']
    issue = int(constants[777]['value'][0])
    ids = constants[770]['value'][:3]
    points = constants[772]['value'][:3]
    if (issue <= 0 or len(ids) != 3 or len(set(ids)) != 3 or len(points) != 3
            or any(not isinstance(value, (int, float)) or value <= 0 or value != int(value) for value in (*ids, *points))):
        raise ValueError('当前绘卷碎片配置无效')
    painting_ids = tuple(str(key) for key, row in sorted(paintings.items()) if row.get('issue') == issue)
    if not painting_ids:
        raise ValueError('当前期次尚无绘卷配置')
    fingerprint = hashlib.sha256(json.dumps([issue, records], sort_keys=True).encode()).hexdigest()
    return Activity(issue, version, fingerprint, painting_ids,
                    dict(zip(('large', 'medium', 'small'), map(int, ids))),
                    dict(zip(('large', 'medium', 'small'), map(int, points))))


def current_activity():
    with _lock:
        version, info, base = parse_version(download(VERSION_URL, 65536))
        metadata = json.loads(download(base + 'static/' + info + '.json', 2 * 1024**2))
        descriptor = metadata['fileinfo']['thd/script3.thx']
        digest = descriptor['md5']
        if not re.fullmatch('[0-9a-f]{32}', digest) or not 0 < descriptor['size'] <= LIMIT:
            raise ValueError('活动资源索引校验信息无效')
        root = storage.ROOT / 'activity'
        path = root / (digest + '.thx')
        data = path.read_bytes() if path.exists() else download(base + 'static/' + digest)
        if len(data) != descriptor['size'] or hashlib.md5(data).hexdigest() != digest:
            raise ValueError('活动资源索引校验失败')
        if not path.exists():
            storage.atomic_write(path, data)
        records = resource_records(data)
        key = (version, tuple((name, row['digest']) for name, row in sorted(records.items())))
        if key not in _parsed:
            tables = {}
            for name, row in records.items():
                digest = row['digest']
                path = root / (digest + '.payload')
                payload = path.read_bytes() if path.exists() else download(base + 'dynamic/' + digest[:2] + '/' + digest[2:])
                if len(payload) != row['size']:
                    raise ValueError('活动配置资源长度不匹配')
                decoded = decode_payload(payload, digest)
                tables[name] = read_table(decoded, name)
                if not path.exists():
                    storage.atomic_write(path, payload)
            activity = build_activity(version, records, tables)
            _parsed.clear()
            _parsed[key] = activity
        return _parsed[key]
