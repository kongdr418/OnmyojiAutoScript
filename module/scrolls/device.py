"""Use OAS configuration, platform discovery and ADB without starting the game."""
import ipaddress
import re
from pathlib import Path
from types import SimpleNamespace

import psutil
from adbutils import AdbError

from module.device.app_control import AppControl
from module.device.connection_attr import ConnectionAttr
from module.device.platform2 import Platform
from module.scrolls.redroid import RedroidFiles
from module.scrolls.storage import binding


class ScrollDevice(Platform, AppControl):
    def __init__(self, config, *, saved_instance=None):
        # Full Device initialization may start an emulator and benchmark screenshots.
        # This read-only query uses its existing connection/platform components.
        ConnectionAttr.__init__(self, config)
        if self.serial == 'auto':
            self.detect_device()
        self.package = config.script.device.package_name.value
        # Remote containers have no Windows emulator installation. Do not let
        # stale MuMu instance settings override their explicitly configured ADB.
        remote = self.is_network_device and not ipaddress.ip_address(self.serial.rsplit(':', 1)[0]).is_loopback
        saved_name = (saved_instance or {}).get('instance')
        normalized = self.serial.replace('localhost:', '127.0.0.1:')
        # Imports already resolved the serial/name pair. Reuse it only while
        # every configured device field still matches its saved binding.
        reuse = (not remote and saved_name and saved_instance.get('binding') == binding(config)
                 and saved_instance.get('serial') == normalized
                 and getattr(config.script.device, 'emulatorinfo_type', '') == 'MuMuPlayer12'
                 and config.script.device.emulatorinfo_name == saved_name)
        if reuse:
            self.instance = SimpleNamespace(type='MuMuPlayer12', name=saved_name, serial=normalized)
        else:
            self.instance = None if remote else self.emulator_instance
        if self.instance is None:
            self.adb_client.connect(self.serial, timeout=5)
            self.redroid_files = RedroidFiles(self)
            if not self.redroid_files.is_redroid():
                raise RuntimeError('无法识别当前设备，绘卷读取支持 MuMu 或 redroid')
            self.instance = SimpleNamespace(type='redroid', name=self.serial, serial=self.serial)
        self.serial = self.instance.serial

    @property
    def supports_live_memory(self):
        return self.instance.type == 'MuMuPlayer12'

    def data_disk(self):
        if self.instance.type != 'MuMuPlayer12':
            raise RuntimeError('本地账号读取当前支持 MuMu')
        instance = self.instance if hasattr(self.instance, 'emulator') else self.emulator_instance
        return Path(instance.emulator.abspath(f'../vms/{instance.name}/data.vdi'))

    def host_pid(self):
        if self.instance.type != 'MuMuPlayer12':
            raise RuntimeError('游戏内存读取当前适配 MuMu，请使用已有的 MuMu 设备配置')
        unreadable = False
        for process in psutil.process_iter(['name']):
            if process.info['name'] != 'MuMuVMMHeadless.exe':
                continue
            try:
                arguments = process.cmdline()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                unreadable = True
                continue
            if '--comment' in arguments:
                position = arguments.index('--comment') + 1
                if position < len(arguments) and arguments[position] == self.instance.name:
                    return process.pid
        if unreadable:
            raise RuntimeError('无法确认 MuMu 进程所属实例，已停止查询，请检查进程访问权限')
        return None

    def game_alive(self):
        if self.instance.type == 'redroid':
            return self.redroid_files.game_alive()
        if self.host_pid() is None:
            return False
        try:
            self.adb_client.connect(self.serial, timeout=3)
        except (AdbError, TimeoutError) as error:
            raise OSError('无法连接当前配置的 ADB 设备，请检查设备地址') from error
        if self.package == 'auto':
            output = self.readonly_shell(['pm', 'list', 'packages', 'com.netease.onmyoji'])
            packages = sorted({line.removeprefix('package:').strip() for line in output.splitlines()
                               if line.startswith('package:com.netease.onmyoji')})
            if len(packages) != 1:
                raise RuntimeError('当前设备未能唯一识别阴阳师，请在 OAS 配置中明确选择游戏包名')
            self.package = packages[0]
        pids = self.readonly_shell(['pidof', self.package]).split()
        if any(not pid.isdigit() for pid in pids):
            raise OSError('无法确认当前设备的游戏进程状态')
        return bool(pids)

    def readonly_shell(self, arguments):
        # Querying must never invoke the automation connection recovery, which
        # can rediscover devices or restart a shared ADB server.
        try:
            return self.adb.shell(arguments, timeout=3).strip()
        except (AdbError, TimeoutError) as error:
            raise OSError('当前配置的设备连接不可用，请检查 ADB 状态') from error

    def gate_address(self):
        package_info = self.readonly_shell(['dumpsys', 'package', self.package])
        match = re.search(r'\buserId=(\d+)', package_info)
        if match is None:
            raise RuntimeError('未能识别游戏进程所属用户，请等待登录完成后重试')
        uid = match[1]
        connections = self.readonly_shell(['ss', '-nte'])
        addresses = set()
        for line in connections.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0] != 'ESTAB' or not re.search(r'\buid:' + uid + r'\b', line):
                continue
            host, separator, port = fields[4].rpartition(':')
            if not separator or not port.isdigit() or not 30000 <= int(port) <= 40000:
                continue
            host = host.strip('[]').removeprefix('::ffff:')
            try:
                if ipaddress.ip_address(host).is_global:
                    addresses.add((host, int(port)))
            except ValueError:
                continue
        if len(addresses) != 1:
            raise RuntimeError('未能唯一识别当前游戏网关，请等待角色登录完成后重试')
        return list(addresses.pop())

    def game_runtime(self):
        pids = self.readonly_shell(['pidof', self.package]).split()
        if not pids or not all(pid.isdigit() for pid in pids):
            raise RuntimeError('游戏进程已退出，请重试查询')
        processes = []
        for pid in pids:
            stat = self.readonly_shell(['cat', f'/proc/{pid}/stat'])
            # The command name can contain spaces and parentheses. Fields
            # following its final ')' begin at proc stat field 3.
            fields = stat.rsplit(')', 1)[-1].split()
            if len(fields) < 20 or not fields[19].isdigit():
                raise RuntimeError('无法校验当前游戏进程，请重试查询')
            processes.append((int(pid), fields[19]))
        return tuple(sorted(processes))
