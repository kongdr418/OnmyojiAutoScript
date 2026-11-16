"""The small BSON subset used by the game's RPC messages."""
import struct

import msgpack


class ObjectId(bytes):
    def __new__(cls, value):
        if len(value) != 12:
            raise ValueError('Invalid ObjectId')
        return super().__new__(cls, value)

    def __str__(self):
        return self.hex()


def encode_document(document):
    fields = []
    for key, value in document.items():
        key = str(key).encode('utf-8')
        if b'\0' in key:
            raise ValueError('Invalid BSON key')
        if value is None:
            kind, payload = 10, b''
        elif isinstance(value, bool):
            kind, payload = 8, bytes([value])
        elif isinstance(value, str):
            text = value.encode('utf-8') + b'\0'
            kind, payload = 2, struct.pack('<i', len(text)) + text
        elif isinstance(value, dict):
            kind, payload = 3, encode_document(value)
        elif isinstance(value, (list, tuple)):
            kind, payload = 4, encode_document(dict(enumerate(value)))
        elif isinstance(value, float):
            kind, payload = 1, struct.pack('<d', value)
        elif isinstance(value, int):
            kind = 16 if -(2**31) <= value < 2**31 else 18
            payload = struct.pack('<i' if kind == 16 else '<q', value)
        elif isinstance(value, ObjectId):
            kind, payload = 7, bytes(value)
        elif isinstance(value, bytes):
            kind, payload = 5, struct.pack('<iB', len(value), 0) + value
        else:
            raise ValueError('Unsupported BSON value')
        fields.append(bytes([kind]) + key + b'\0' + payload)
    body = b''.join(fields) + b'\0'
    return struct.pack('<i', len(body) + 4) + body


class DocumentReader:
    def __init__(self, data):
        self.data = data
        self.position = 0

    def take(self, size):
        if size < 0 or self.position + size > len(self.data):
            raise ValueError('Incomplete BSON value')
        value = self.data[self.position:self.position + size]
        self.position += size
        return value

    def unpack(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def document(self, depth=0, array=False):
        if depth > 64:
            raise ValueError('BSON nesting limit exceeded')
        end = self.position + self.unpack('<i')
        if end > len(self.data) or end < self.position + 1:
            raise ValueError('Invalid BSON length')
        values = {}
        while self.position < end - 1:
            kind = self.unpack('<B')
            key_end = self.data.find(b'\0', self.position, end)
            if key_end < 0:
                raise ValueError('Invalid BSON key')
            key = self.take(key_end - self.position).decode('utf-8')
            self.take(1)
            if kind == 1:
                value = self.unpack('<d')
            elif kind == 2:
                raw = self.take(self.unpack('<i'))
                if not raw.endswith(b'\0'):
                    raise ValueError('Invalid BSON string')
                value = raw[:-1].decode('utf-8')
            elif kind in (3, 4):
                value = self.document(depth + 1, kind == 4)
            elif kind == 5:
                size = self.unpack('<i')
                self.take(1)
                value = self.take(size)
            elif kind == 7:
                value = ObjectId(self.take(12))
            elif kind == 8:
                value = bool(self.unpack('<B'))
            elif kind == 10:
                value = None
            elif kind == 16:
                value = self.unpack('<i')
            elif kind in (9, 17, 18):
                value = self.unpack('<q')
            else:
                raise ValueError('Unsupported BSON type')
            values[key] = value
        if self.position != end - 1 or self.take(1) != b'\0':
            raise ValueError('Invalid BSON document boundary')
        return list(values.values()) if array else values


def decode_extension(code, data):
    return ObjectId(data) if code == 42 and len(data) == 12 else msgpack.ExtType(code, data)


def decode_parameters(data):
    if len(data) >= 5 and struct.unpack_from('<I', data)[0] == len(data):
        reader = DocumentReader(data)
        result = reader.document()
        if reader.position != len(data):
            raise ValueError('Trailing BSON bytes')
        return result
    return msgpack.unpackb(data, raw=False, strict_map_key=False, ext_hook=decode_extension)
