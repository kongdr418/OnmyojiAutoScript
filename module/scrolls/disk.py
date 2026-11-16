"""Bounded, read-only access to MuMu dynamic VDI userdata files."""
from pathlib import Path
import struct


class Vdi:
    def __init__(self, path):
        self.stream = Path(path).open('rb', buffering=0)
        try:
            header = self.stream.read(512)
            if len(header) != 512 or struct.unpack_from('<I', header, 64)[0] != 0xbeda107f:
                raise ValueError('不支持的 MuMu 磁盘格式')
            version, header_size, image_type = struct.unpack_from('<III', header, 68)
            if version != 0x10001 or header_size < 400 or image_type not in (1, 2):
                raise ValueError('不支持的 MuMu 磁盘版本')
            self.offset_blocks, self.offset_data = struct.unpack_from('<II', header, 340)
            self.size = struct.unpack_from('<Q', header, 368)[0]
            self.block_size, self.extra, blocks = struct.unpack_from('<III', header, 376)
            if (not 4096 <= self.block_size <= 16 * 1024**2 or self.extra > 4096
                    or not 0 < blocks <= 2**20 or self.size > blocks * self.block_size
                    or self.offset_blocks < 512 or self.offset_data < self.offset_blocks + blocks * 4):
                raise ValueError('MuMu 磁盘头无效')
            self.stream.seek(self.offset_blocks)
            raw = self.stream.read(blocks * 4)
            if len(raw) != blocks * 4:
                raise ValueError('MuMu 磁盘索引不完整')
            self.mapping = struct.unpack('<' + 'I' * blocks, raw)
        except Exception:
            self.close()
            raise

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self, offset, size):
        if offset < 0 or not 0 <= size <= 32 * 1024**2 or offset + size > self.size:
            raise ValueError('MuMu 磁盘读取超出范围')
        result = bytearray()
        while size:
            block, within = divmod(offset, self.block_size)
            count = min(size, self.block_size - within)
            physical = self.mapping[block]
            if physical >= 0xfffffffe:
                result.extend(b'\0' * count)
            else:
                self.stream.seek(self.offset_data + physical * (self.block_size + self.extra) + self.extra + within)
                data = self.stream.read(count)
                if len(data) != count:
                    raise ValueError('MuMu 磁盘正在变化或读取不完整，请重试')
                result.extend(data)
            offset += count
            size -= count
        return bytes(result)


class Ext4:
    def __init__(self, disk, offset):
        self.disk, self.offset = disk, offset
        header = self.read(1024, 1024)
        if struct.unpack_from('<H', header, 56)[0] != 0xef53:
            raise ValueError('未识别到 ext4 用户数据')
        exponent = struct.unpack_from('<I', header, 24)[0]
        if exponent > 6:
            raise ValueError('不支持的 ext4 块大小')
        self.block_size = 1024 << exponent
        self.inodes_per_group = struct.unpack_from('<I', header, 40)[0]
        self.inode_count = struct.unpack_from('<I', header)[0]
        self.inode_size = struct.unpack_from('<H', header, 88)[0]
        self.desc_size = max(32, struct.unpack_from('<H', header, 254)[0])
        self.desc_table = (struct.unpack_from('<I', header, 20)[0] + 1) * self.block_size
        if (not self.inodes_per_group or not self.inode_count or
                not 128 <= self.inode_size <= self.block_size or not 32 <= self.desc_size <= self.block_size):
            raise ValueError('ext4 文件系统参数无效')

    def read(self, offset, size):
        return self.disk.read(self.offset + offset, size)

    def inode(self, number):
        if not 1 <= number <= self.inode_count:
            raise ValueError('ext4 inode 无效')
        group, index = divmod(number - 1, self.inodes_per_group)
        descriptor = self.read(self.desc_table + group * self.desc_size, self.desc_size)
        table = struct.unpack_from('<I', descriptor, 8)[0]
        if self.desc_size >= 64:
            table |= struct.unpack_from('<I', descriptor, 40)[0] << 32
        return self.read(table * self.block_size + index * self.inode_size, self.inode_size)

    def extents(self, data, parent_depth=6):
        magic, count, maximum, depth = struct.unpack_from('<4H', data)
        if magic != 0xf30a or count > maximum or 12 + count * 12 > len(data) or depth >= parent_depth:
            raise ValueError('ext4 extent 树无效')
        for index in range(count):
            entry = data[12 + index * 12:24 + index * 12]
            if depth:
                _, low, high = struct.unpack_from('<IIH', entry)
                yield from self.extents(self.read((low | high << 32) * self.block_size, self.block_size), depth)
            else:
                logical, size, high, low = struct.unpack('<IHHI', entry)
                yield logical, size - 0x8000 if size > 0x8000 else size, low | high << 32, size > 0x8000

    def content(self, number, limit=16 * 1024**2):
        inode = self.inode(number)
        size = struct.unpack_from('<I', inode, 4)[0] | struct.unpack_from('<I', inode, 108)[0] << 32
        if size > limit or not struct.unpack_from('<I', inode, 32)[0] & 0x80000:
            raise ValueError('本地文件过大或使用不支持的 ext4 存储方式')
        result = bytearray(size)
        visited = 0
        for logical, blocks, physical, unwritten in self.extents(inode[40:100]):
            visited += 1
            if visited > 16384:
                raise ValueError('ext4 extent 数量超出范围')
            start = logical * self.block_size
            if start >= size or unwritten:
                continue
            take = min(blocks * self.block_size, size - start)
            result[start:start + take] = self.read(physical * self.block_size, take)
        return bytes(result)

    def directory(self, number):
        data = self.content(number)
        result = {}
        offset = 0
        while offset + 8 <= len(data):
            inode, length, name_size, kind = struct.unpack_from('<IHBB', data, offset)
            if length < 8 or length % 4 or offset + length > len(data) or name_size > length - 8:
                raise ValueError('ext4 目录记录无效')
            if inode:
                name = data[offset + 8:offset + 8 + name_size].decode('utf-8')
                result[name] = (inode, kind)
            offset += length
        return result

    def lookup(self, path):
        number = 2
        for part in path.strip('/').split('/'):
            if part:
                number = self.directory(number)[part][0]
        return number

    def file(self, path):
        return self.content(self.lookup(path))


def userdata(disk):
    # MuMu userdata is normally MiB aligned. Include GPT partition starts too.
    offsets = set(range(0, min(disk.size - 2048, 256 * 1024**2), 1024**2))
    header = disk.read(512, 512)
    if header[:8] == b'EFI PART':
        table, count, size = struct.unpack_from('<QII', header, 72)
        if 0 < count <= 256 and 128 <= size <= 4096:
            entries = disk.read(table * 512, count * size)
            for index in range(count):
                entry = entries[index * size:(index + 1) * size]
                if entry[:16] != b'\0' * 16:
                    offsets.add(struct.unpack_from('<Q', entry, 32)[0] * 512)
    for offset in sorted(offsets):
        if offset + 2048 > disk.size or disk.read(offset + 1080, 2) != b'\x53\xef':
            continue
        fs = Ext4(disk, offset)
        root = fs.directory(2)
        if 'data' in root and 'media' in root:
            return fs
    raise ValueError('未找到支持的 MuMu ext4 用户磁盘')
