"""Bounded, read-only ADB access to saved game files in a redroid container."""
import re
import shlex
import subprocess


class RedroidFiles:
    def __init__(self, device):
        self.device = device
        self.root = None

    def command(self, arguments, *, allow_failure=False):
        try:
            # Shell v2 preserves remote exit codes; exec-out can report success
            # when pidof or a private-file read actually failed. Disable PTY.
            result = subprocess.run(
                [self.device.adb_binary, '-s', self.device.serial, 'shell', '-T', shlex.join(arguments)],
                capture_output=True, timeout=10,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError('redroid ADB 读取超时，请检查设备连接') from error
        if result.returncode and not allow_failure:
            # Never include command output: SDK files contain private credentials.
            raise OSError('redroid 文件读取失败，请检查 ADB 连接及游戏文件权限')
        return result

    def is_redroid(self):
        model = self.command(['getprop', 'ro.product.model']).stdout
        return b'redroid' in model.lower()

    def root_command(self, arguments):
        if self.root is None:
            if self.command(['id', '-u']).stdout.strip() == b'0':
                self.root = []
            elif self.command(['su', '0', 'id', '-u'], allow_failure=True).stdout.strip() == b'0':
                self.root = ['su', '0']
            else:
                raise RuntimeError('redroid 读取账号需要 Root 文件权限；不会自动重启 ADB 或修改容器')
        return self.command(self.root + arguments).stdout

    def packages(self):
        if self.device.package != 'auto':
            packages = [self.device.package]
        else:
            output = self.command(['pm', 'list', 'packages', 'com.netease.onmyoji']).stdout.decode('utf-8')
            packages = [line.removeprefix('package:').strip() for line in output.splitlines()]
        packages = sorted(set(packages))
        if not packages or len(packages) > 32 or any(
                not re.fullmatch(r'com\.netease\.onmyoji[\w.]*', package, flags=re.ASCII) for package in packages):
            raise ValueError('未找到阴阳师安装包，请检查当前配置的游戏包名')
        return packages

    def names(self, path):
        output = self.root_command(['ls', '-1', path])
        if len(output) > 64 * 1024:
            raise ValueError('redroid 游戏文件列表过大')
        names = output.decode('utf-8').splitlines()
        if len(names) > 512:
            raise ValueError('redroid 游戏文件数量超出范围')
        return names

    def read(self, path):
        limit = 8 * 1024**2
        data = self.root_command(['head', '-c', str(limit + 1), path])
        if len(data) > limit:
            raise ValueError('redroid 游戏本地记录过大')
        return data

    def game_alive(self):
        for package in self.packages():
            result = self.command(['pidof', package], allow_failure=True)
            if result.returncode not in (0, 1):
                raise OSError('无法检查 redroid 游戏运行状态，请检查 ADB 连接')
            pids = result.stdout.split()
            if pids and all(pid.isdigit() for pid in pids):
                return True
            if result.returncode == 0 or pids or result.stderr:
                raise OSError('无法确认 redroid 游戏运行状态，请重试')
        return False
