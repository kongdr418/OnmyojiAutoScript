"""Resolve server-specific gateways from the game's public HTTPS server list."""
import ipaddress
import threading
import time

import requests


SERVER_LIST_URL = 'https://g37.update.netease.com/mini_server_list_cn.txt'
_cache = {}
_expires = 0
_lock = threading.Lock()


def parse_servers(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 6 or not fields[4].isdigit():
            continue
        address = fields[5].split(':')
        if len(address) != 4 or not address[1].isdigit():
            continue
        try:
            host = ipaddress.ip_address(address[0])
        except ValueError:
            continue
        port = int(address[1])
        if host.is_global and 1 <= port <= 65535:
            result.setdefault(fields[4], []).append([str(host), port])
    if not result:
        raise ValueError('官方区服列表格式已变化')
    return result


def gate_address(server_id):
    global _cache, _expires
    with _lock:
        if time.monotonic() >= _expires:
            with requests.get(SERVER_LIST_URL, timeout=(5, 8), stream=True) as response:
                response.raise_for_status()
                data = bytearray()
                for block in response.iter_content(65536):
                    data.extend(block)
                    if len(data) > 2 * 1024**2:
                        raise ValueError('官方区服列表过大')
            _cache = parse_servers(data.decode('utf-8'))
            _expires = time.monotonic() + 300
        addresses = _cache.get(str(server_id))
        if not addresses:
            raise ValueError('官方列表中未找到该区服，请重新读取角色记录')
        return list(addresses[0])
