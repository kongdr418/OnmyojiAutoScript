"""Read the data fields in the game's Python 3.11 marshal configuration files."""
from dataclasses import dataclass
import struct


LIMIT = 8 * 1024**2
UNRESOLVED = object()


@dataclass(frozen=True)
class CodeData:
    filename: str
    constants: tuple


class MarshalReader:
    def __init__(self, data):
        if not isinstance(data, bytes) or len(data) > LIMIT:
            raise ValueError('活动配置序列化长度无效')
        self.data = data
        self.position = 0
        self.references = []

    def take(self, size):
        if size < 0 or self.position + size > len(self.data):
            raise ValueError('活动配置序列化数据不完整')
        value = self.data[self.position:self.position + size]
        self.position += size
        return value

    def integer(self):
        return struct.unpack('<i', self.take(4))[0]

    def count(self):
        count = self.integer()
        if not 0 <= count <= 100000:
            raise ValueError('活动配置序列化元素过多')
        return count

    def code(self, depth):
        # CPython 3.11 stores five raw header integers, followed by marshal
        # objects. Bytecode is consumed as bytes and discarded.
        header = struct.unpack('<5i', self.take(20))
        if any(value < 0 for value in header[:4]):
            raise ValueError('活动配置代码头无效')
        bytecode = self.object(depth + 1)
        constants = self.object(depth + 1)
        names = self.object(depth + 1)
        local_names = self.object(depth + 1)
        local_kinds = self.object(depth + 1)
        filename = self.object(depth + 1)
        name = self.object(depth + 1)
        qualified_name = self.object(depth + 1)
        line = self.integer()
        line_table = self.object(depth + 1)
        exception_table = self.object(depth + 1)
        if (not all(isinstance(value, bytes) for value in (bytecode, local_kinds, line_table, exception_table))
                or not all(isinstance(value, tuple) for value in (constants, names, local_names))
                or not all(isinstance(value, str) for value in (filename, name, qualified_name))
                or len(local_names) != len(local_kinds) or line < 0):
            raise ValueError('活动配置代码字段无效')
        return CodeData(filename, constants)

    def object(self, depth=0):
        if depth > 64:
            raise ValueError('活动配置序列化嵌套过深')
        marker = self.take(1)[0]
        kind = chr(marker & 0x7f)
        slot = None
        if marker & 0x80:
            if len(self.references) >= 100000:
                raise ValueError('活动配置序列化引用过多')
            slot = len(self.references)
            self.references.append(UNRESOLVED)
        if kind == 'r':
            index = self.integer()
            if slot is not None or not 0 <= index < len(self.references) or self.references[index] is UNRESOLVED:
                raise ValueError('活动配置序列化引用无效')
            return self.references[index]
        if kind == 'N':
            value = None
        elif kind in ('F', 'T'):
            value = kind == 'T'
        elif kind == 'i':
            value = self.integer()
        elif kind == 'l':
            count = self.integer()
            if abs(count) > 4096:
                raise ValueError('活动配置整数过大')
            value = 0
            for index in range(abs(count)):
                digit = struct.unpack('<H', self.take(2))[0]
                if digit >= 32768:
                    raise ValueError('活动配置整数编码无效')
                value |= digit << (index * 15)
            if count < 0:
                value = -value
        elif kind == 'g':
            value = struct.unpack('<d', self.take(8))[0]
        elif kind == 's':
            value = self.take(self.integer())
        elif kind in ('u', 't', 'a', 'A', 'z', 'Z'):
            size = self.take(1)[0] if kind in ('z', 'Z') else self.integer()
            encoding = 'ascii' if kind in ('a', 'A', 'z', 'Z') else 'utf-8'
            value = self.take(size).decode(encoding, errors='surrogatepass')
        elif kind in ('(', ')'):
            count = self.take(1)[0] if kind == ')' else self.count()
            value = tuple(self.object(depth + 1) for _ in range(count))
        elif kind == 'c':
            value = self.code(depth)
        else:
            raise ValueError('活动配置序列化格式已变化')
        if slot is not None:
            self.references[slot] = value
        return value


def read_code_data(data):
    reader = MarshalReader(data)
    result = reader.object()
    if not isinstance(result, CodeData) or reader.position != len(data):
        raise ValueError('活动配置序列化根对象无效')
    return result
