"""Independent authenticated connection to the verified drawing gateway."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import hashlib
import math
import os
import socket
import struct
import time
import uuid
import zlib

from google.protobuf import descriptor_pool, message_factory
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.serialization import load_pem_public_key
try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
except ImportError:
    from cryptography.hazmat.primitives.ciphers.algorithms import ARC4
from module.scrolls.codec import encode_document, decode_parameters
from module.scrolls.activity import current_activity

ROOT = Path(__file__).resolve().parent
POOL = descriptor_pool.DescriptorPool()
for descriptor_name in ("common", "client_gate"):
    POOL.AddSerializedFile((ROOT / f"{descriptor_name}.descriptor").read_bytes())


def make_message(name, **fields):
    descriptor = POOL.FindMessageTypeByName(f"mobile.server.{name}")
    return message_factory.GetMessageClass(descriptor)(**fields)


def method_digest(name):
    return hashlib.md5(name.encode("ascii")).digest()


class Gateway:
    def __init__(self, connection):
        self.connection = connection
        self.encryptor = None
        self.decryptor = None
        self.compressor = None
        self.decompressor = None
        self.buffer = b""
        self.deadline = time.monotonic() + 55

    def send(self, index, message):
        payload = message.SerializeToString()
        data = struct.pack("<IH", len(payload) + 2, index) + payload
        if self.compressor is not None:
            data = self.compressor.compress(data)
            data += self.compressor.flush(zlib.Z_SYNC_FLUSH)
        if self.encryptor is not None:
            data = self.encryptor.update(data)
        self.connection.sendall(data)

    def receive(self, timeout):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Drawing query deadline exceeded')
        deadline = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < deadline:
            frames = []
            while len(self.buffer) >= 4:
                size = struct.unpack_from("<I", self.buffer)[0]
                if not 2 <= size <= 16 * 1024 * 1024:
                    raise ValueError("Invalid server frame length")
                if len(self.buffer) < size + 4:
                    break
                index = struct.unpack_from("<H", self.buffer, 4)[0]
                frames.append((index, self.buffer[6:size + 4]))
                self.buffer = self.buffer[size + 4:]
            if frames:
                return frames
            self.connection.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                data = self.connection.recv(65536)
            except TimeoutError:
                return []
            if not data:
                raise ConnectionError("The server closed the connection")
            if self.decryptor is not None:
                data = self.decryptor.update(data)
            if self.decompressor is not None:
                data = self.decompressor.decompress(data, 16 * 1024 * 1024)
                if self.decompressor.unconsumed_tail:
                    raise ValueError('Server decompressed response exceeds size limit')
            self.buffer += data
            if len(self.buffer) > 16 * 1024 * 1024:
                raise ValueError('Server response exceeds size limit')
        return []

    def wait_for_entity(self, name, timeout=20):
        deadline = time.monotonic() + timeout
        expected = method_digest(name)
        while time.monotonic() < deadline:
            for index, payload in self.receive(deadline - time.monotonic()):
                if index == 3:
                    entity = make_message("EntityInfo")
                    entity.ParseFromString(payload)
                    if entity.type.md5 == expected:
                        return entity
        raise TimeoutError(f"The server did not create {name}")

    def call_entity(self, entity, method, parameters):
        request = make_message(
            "EntityMessage", id=entity.id, parameters=encode_document(parameters)
        )
        request.method.md5 = method_digest(method)
        self.send(3, request)

    def wait_for_callback(self, name, timeout=12):
        deadline = time.monotonic() + timeout
        expected = method_digest(name)
        while time.monotonic() < deadline:
            for index, payload in self.receive(deadline - time.monotonic()):
                if index == 5:
                    reply = make_message("EntityMessage")
                    reply.ParseFromString(payload)
                    if reply.method.md5 == expected:
                        return decode_parameters(reply.parameters)
        raise TimeoutError(f"The server did not return {name}")

    def establish(self):
        self.send(0, make_message("Void"))
        frames = self.receive(5)
        if len(frames) != 1 or frames[0][0] != 0:
            raise ValueError("Unexpected session seed response")
        seed = make_message("SessionSeed")
        seed.ParseFromString(frames[0][1])
        session_key = os.urandom(20)
        session = make_message(
            "SessionKey",
            random_padding_header=os.urandom(32),
            session_key=session_key,
            seed=seed.seed,
            random_padding_tail=os.urandom(32),
        )
        public_key = load_pem_public_key((ROOT / "gate-login-public.pem").read_bytes())
        encrypted = public_key.encrypt(
            session.SerializeToString(),
            padding.OAEP(
                mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None
            ),
        )
        self.send(1, make_message("EncryptString", encryptstr=encrypted))
        self.encryptor = Cipher(ARC4(session_key), mode=None).encryptor()
        self.decryptor = Cipher(ARC4(session_key), mode=None).decryptor()
        if not any(index == 1 for index, _ in self.receive(5)):
            raise ValueError("Session key exchange failed")
        self.send(
            2,
            make_message(
                "ConnectServerRequest", type=0,
                deviceid=("drawing-query-" + uuid.uuid4().hex).encode("ascii"),
            ),
        )
        self.compressor = zlib.compressobj()
        self.decompressor = zlib.decompressobj()
        return self.wait_for_entity("ClientAccount", timeout=8)


def now_string():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def progress_rows(progress, activity=None):
    info = progress.get('server_info')
    total = progress.get('total_score')
    if not isinstance(info, dict) or not isinstance(total, (int, float)) or not math.isfinite(total) or total <= 0:
        raise ValueError('绘卷进度响应不完整')
    if activity is not None:
        if not set(activity.painting_ids).issubset(map(str, info)):
            raise ValueError('服务器绘卷期次与当前活动配置不一致，请等待区服更新后重试')
        info = {str(key): value for key, value in info.items() if str(key) in activity.painting_ids}
    elif len(info) != 6:
        raise ValueError('当前版本需要六卷活动数据')
    rows = []
    for ordinal, painting_id in enumerate(sorted(info, key=lambda value: int(value)), 1):
        score = info[painting_id].get('score')
        if not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0:
            raise ValueError('绘卷积分格式无效')
        rows.append({
            'scroll': ordinal, 'painting_id': str(painting_id), 'score': score,
            'percent': min(score / total * 100, 100),
            'rank_20_score': None, 'rank_100_score': None, 'rankings': [],
        })
    return rows


def fragment_counts(currency, item_ids=None):
    if not isinstance(currency, dict) or not item_ids:
        return None
    result = {}
    for size, item_id in item_ids.items():
        count = currency.get(str(item_id), currency.get(item_id, 0))
        if not isinstance(count, (int, float)) or not math.isfinite(count) or count < 0 or count != int(count):
            raise ValueError('绘卷碎片数量格式无效')
        result[size] = int(count)
    return result


def query_details(account, address, strict_role=False, activity=None):
    activity = activity or current_activity()
    account = {**account, 'patch_version': activity.version}
    with socket.create_connection(tuple(address), timeout=8) as connection:
        gateway = Gateway(connection)
        entity = gateway.establish()
        gateway.call_entity(entity, 'fetch_hotfix', {'smd5': ''})
        gateway.wait_for_callback('fetch_hotfix_cb')
        gateway.call_entity(entity, 'login_with_sdk', {'account_info': account})
        login = gateway.wait_for_callback('on_login_result')
        if login.get('ec') != 0:
            raise PermissionError('账号授权失效，请登录一次游戏后点击查询，重新获取授权')
        avatars = login.get('avatar_list', {})
        roles = list(avatars.values()) if isinstance(avatars, dict) else avatars
        if not isinstance(roles, list):
            raise ValueError('账号角色列表无效')
        selected = [role for role in roles if str(role.get('avatarId')) == str(account.get('user_id'))]
        if not selected and len(roles) == 1 and not strict_role:
            selected = roles
        if len(selected) != 1:
            raise ValueError('未找到所选角色，请重新读取本地账号并确认区服')
        role = selected[0]
        if strict_role and role.get('sid') is not None and str(role['sid']) != str(account['serverid']):
            raise ValueError('服务器返回的角色区服与所选区服不一致')
        characters = role.get('yys', [])
        if not characters or not isinstance(characters[0], int):
            raise ValueError('角色信息不完整')
        gateway.call_entity(entity, 'select_role', {
            'id': characters[0], 'avatar_id': str(role['avatarId']),
            'client_cache_data_md5': {'rpc_static_index_map_md5': hashlib.md5(b'').hexdigest()},
        })
        avatar = gateway.wait_for_entity('ClientAvatar')
        avatar_info = decode_parameters(avatar.info)
        if strict_role and avatar_info.get('serverid') is not None and str(avatar_info['serverid']) != str(account['serverid']):
            raise ValueError('服务器返回的角色区服与所选区服不一致')
        inventory = fragment_counts(avatar_info.get('currency'), activity.fragment_ids)
        gateway.call_entity(avatar, 'pic_get_info', {})
        progress = gateway.wait_for_callback('pic_get_info_cb')
        rows = progress_rows(progress, activity)
        warnings = []
        for row in rows:
            service = 'pic_' + row['painting_id']
            gateway.call_entity(avatar, 'get_rank_list', {'s_name': service, 'index': 0, 'count': 100})
            try:
                reply = gateway.wait_for_callback('get_rank_list_cb', timeout=5)
                if reply.get('service_name', reply.get('s_name', service)) != service:
                    raise ValueError('绘卷排行榜编号不匹配')
                scores = [entry['score'] for entry in reply['infos']]
                if len(scores) > 100 or any(not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0 for score in scores):
                    raise ValueError('绘卷排行榜积分格式无效')
                if scores != sorted(scores, reverse=True):
                    raise ValueError('绘卷排行榜顺序异常')
                row['rankings'] = [{'rank': index, 'score': score} for index, score in enumerate(scores, 1)]
                row['rank_20_score'] = scores[19] if len(scores) >= 20 else None
                row['rank_100_score'] = scores[99] if len(scores) >= 100 else None
            except (TimeoutError, ValueError, KeyError, TypeError):
                warnings.append(f"卷{row['scroll']}排行榜暂未取得")
        return {
            'identity': {'role_id': str(role['avatarId']), 'name': role.get('name', ''),
                         'server_id': str(avatar_info.get('serverid') or role.get('sid') or account.get('serverid', ''))},
            'source': 'independent', 'freshness': 'server', 'queried_at': now_string(),
            'scrolls': rows, 'inventory': inventory, 'warnings': warnings,
            'activity': activity.metadata(),
        }
