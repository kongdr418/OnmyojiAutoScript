"""Strict, zero-execution reader for the observed Onmyoji Bindict tables.

This implementation is based on byte-level observations of this client's
tables. Public LifeAfter toolkit format notes were consulted for schema,
bitmap and bucket concepts; no game code is imported or executed.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct


class FormatError(ValueError):
    pass


@dataclass
class MapValue:
    pairs: list[tuple[object, object]]


class BindictReader:
    def __init__(self, data: bytes, native_containers=False):
        self.data = data
        self.native_containers = native_containers
        self.cache = {}
        self.active = set()
        self.schemas = {}
        self.tags = {}
        self.reference_count = 0
        if len(data) < 8:
            raise FormatError("missing string-pool header")
        count = struct.unpack_from("<I", data)[0]
        pool_start = 8 + count * 4
        if pool_start > len(data):
            raise FormatError("string-pool offsets out of range")
        offsets = struct.unpack_from(f"<{count+1}I", data, 4)
        if offsets[0] != 0 or any(a > b for a, b in zip(offsets, offsets[1:])):
            raise FormatError("non-monotone string-pool offsets")
        pool_end = pool_start + offsets[-1]
        if pool_end + 4 > len(data):
            raise FormatError("string pool exceeds file")
        self.strings = [data[pool_start+a:pool_start+b].decode("utf-8")
                        for a, b in zip(offsets, offsets[1:])]
        self.blob = data[pool_end:]
        self.index_start = struct.unpack_from("<I", self.blob)[0]
        if not 4 <= self.index_start < len(self.blob):
            raise FormatError("invalid root index offset")

    def check(self, pos, size=1, limit=None):
        limit = len(self.blob) if limit is None else limit
        if pos < 0 or size < 0 or pos + size > limit:
            raise FormatError(f"range {pos}+{size} exceeds {limit}")

    def byte(self, pos, limit=None):
        self.check(pos, 1, limit)
        return self.blob[pos], pos+1

    def varint(self, pos, limit=None):
        value = 0
        for shift in range(0, 70, 7):
            part, pos = self.byte(pos, limit)
            if shift == 63 and part > 1:
                raise FormatError("varint exceeds unsigned 64-bit range")
            value |= (part & 127) << shift
            if not part & 128:
                return value, pos
        raise FormatError("unterminated varint")

    def string(self, slot):
        if not 0 <= slot < len(self.strings):
            raise FormatError(f"string slot {slot} out of range")
        return self.strings[slot]

    def at(self, pos):
        if pos in self.cache:
            return self.cache[pos]
        if pos in self.active or len(self.active) >= 100:
            raise FormatError(f"cyclic/excessively deep reference at {pos}")
        self.check(pos)
        self.active.add(pos)
        try:
            value, end = self.value(pos)
            if end > self.index_start:
                raise FormatError(f"referenced value at {pos} crosses root index")
            self.cache[pos] = (value, end)
            return value, end
        finally:
            self.active.remove(pos)

    def schema(self, pos):
        if pos in self.schemas:
            return self.schemas[pos]
        count, cursor = self.varint(pos, self.index_start)
        bits, cursor = self.varint(cursor, self.index_start)
        if not 0 < count <= 512 or bits > count:
            raise FormatError(f"invalid schema {pos}: count={count}, bits={bits}")
        fields = []
        for _ in range(count):
            slot, cursor = self.varint(cursor, self.index_start)
            tag, cursor = self.byte(cursor, self.index_start)
            fields.append((self.string(slot), tag))
        # A shared schema can contain several types for the same field.
        # Its bitmap selects one; reject duplicates only after selection.
        self.schemas[pos] = fields, bits
        return fields, bits

    def value(self, pos, tag=None, limit=None):
        limit = len(self.blob) if limit is None else limit
        if tag is None:
            tag, pos = self.byte(pos, limit)
        self.tags[tag] = self.tags.get(tag, 0) + 1
        kind, width = tag & 15, tag & 240
        if kind == 1:
            result, pos = self.varint(pos, limit)
            if width == 0x10:
                result = (result >> 1) ^ -(result & 1)
            elif width != 0:
                raise FormatError(f"unsupported integer tag {tag:#x}")
            return result, pos
        if kind == 2:
            if width not in (0x10, 0x20):
                raise FormatError(f"unsupported float tag {tag:#x}")
            size, fmt = (4, "<f") if width == 0x10 else (8, "<d")
            self.check(pos, size, limit)
            return struct.unpack_from(fmt, self.blob, pos)[0], pos + size
        if kind == 3 and width == 0:
            result, pos = self.byte(pos, limit)
            if result not in (0, 1):
                raise FormatError(f"invalid boolean {result}")
            return bool(result), pos
        if kind == 4 and width == 0:
            return None, pos
        if kind == 5 and width == 0:
            slot, pos = self.varint(pos, limit)
            return self.string(slot), pos
        if kind == 11 and width == 0:
            offset, pos = self.varint(pos, limit)
            if not 4 <= offset < self.index_start:
                raise FormatError(f"value reference {offset} outside value region")
            self.reference_count += 1
            result, _ = self.at(offset)
            return result, pos
        if kind == 7 and not tag & 0xc0:
            common = None
            if tag & 0x20:
                common, pos = self.byte(pos, limit)
            count, pos = self.varint(pos, limit)
            if count > 1000000:
                raise FormatError("unreasonable array length")
            result = []
            for _ in range(count):
                item, pos = self.value(pos, common, limit)
                result.append(item)
            return (tuple(result) if self.native_containers else result), pos
        if kind == 6 and tag & 0x80:
            schema_offset, pos = self.varint(pos, limit)
            fields, bits = self.schema(schema_offset)
            mask_size = (bits+7)//8
            if tag & 0x40:
                mask_offset, pos = self.varint(pos, limit)
            else:
                mask_offset = pos
                pos += mask_size
            self.check(mask_offset, mask_size, self.index_start)
            mask = self.blob[mask_offset:mask_offset+mask_size]
            result = {}
            for i, (name, field_tag) in enumerate(fields):
                if i >= bits or (mask[i//8] >> (i % 8)) & 1:
                    if name in result:
                        raise FormatError(f"duplicate enabled field {name}")
                    result[name], pos = self.value(pos, field_tag, limit)
            return result, pos
        if tag == 0x36:
            key_tag, pos = self.byte(pos, limit)
            value_tag, pos = self.byte(pos, limit)
            count, pos = self.varint(pos, limit)
            pairs = []
            for _ in range(count):
                key, pos = self.value(pos, key_tag, limit)
                val, pos = self.value(pos, value_tag, limit)
                pairs.append((key, val))
            return MapValue(pairs), pos
        if tag == 0x06:
            count, pos = self.varint(pos, limit)
            if count == 0:
                return {}, pos
            raise FormatError("non-empty inline dictionary is not supported")
        raise FormatError(f"unsupported tag {tag:#04x} at {pos-1}")

    def rows(self):
        pos = self.index_start
        marker, pos = self.byte(pos)
        if marker == 0x46:
            count, pos = self.varint(pos)
            if count == 0 and pos == len(self.blob):
                return []
            raise FormatError('Nonempty compact root requires explicit format support')
        if marker != 0x76:
            raise FormatError(f"unsupported root index tag {marker:#04x}")
        key_tag, pos = self.byte(pos)
        value_tag, pos = self.byte(pos)
        count, pos = self.varint(pos)
        self.check(pos, count*8)
        buckets = [struct.unpack_from("<II", self.blob, pos+i*8) for i in range(count)]
        hashes = [digest for digest, _ in buckets]
        # Different keys can share a 32-bit hash (observed in the original
        # monster table); their distinct offsets remain separate entries.
        if any(a > b for a, b in zip(hashes, hashes[1:])):
            raise FormatError("bucket hashes are not sorted")
        nodes_start = pos+count*8
        node_offsets = sorted(set(offset for _, offset in buckets))
        if len(node_offsets) != count or (node_offsets and node_offsets[0] != nodes_start):
            raise FormatError("non-contiguous/duplicate bucket directory")
        if any(not nodes_start <= n < len(self.blob) for n in node_offsets):
            raise FormatError("bucket outside node region")
        rows = []
        ends = dict(zip(node_offsets, node_offsets[1:] + [len(self.blob)]))
        order = [offset for _, offset in buckets] if self.native_containers else node_offsets
        for cursor in order:
            end = ends[cursor]
            while cursor < end:
                key, cursor = self.value(cursor, key_tag, end)
                offset = cursor
                if value_tag == 0x0b:
                    offset, cursor = self.varint(cursor, end)
                    if not 4 <= offset < self.index_start:
                        raise FormatError("root row outside value region")
                    value, value_end = self.at(offset)
                    if value_end > self.index_start:
                        raise FormatError("row crosses root index")
                else:
                    value, cursor = self.value(cursor, value_tag, end)
                    value_end = cursor
                rows.append(dict(key=key, value=value, offset=offset, end=value_end))
            if cursor != end:
                raise FormatError("bucket did not close")
        return rows
