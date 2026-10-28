# This Python file uses the following encoding: utf-8
"""模拟器画面截取 worker。

直接用 adb 抓一帧，不走 Device / 图像服务。

原因：Device.screenshot() 每次都会向图像服务注册一帧
（module/device/screenshot.py），而服务端对同一配置只保留最新一帧
（module/image/runtime.py::register_frame 会删掉旧的），于是会把正在运行的
任务手里的那一帧删掉，任务下一步做匹配就 KeyError: Unknown frame id。
抓画面是纯旁路功能，不该有这种副作用。
顺带也省掉了 Device 初始化时探包名、探屏幕尺寸的两次截图。

用法：python -m module.server.screen_capture_worker <配置名> <输出文件>
退出码 0 表示成功，错误信息写到 stderr。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path('./config')
# 真机 2780x1264 抓一张 PNG 要 6 秒左右，留一倍余量
ADB_TIMEOUT_SECONDS = 12


def _configured_serial(config_name: str) -> str:
    """读配置里的模拟器地址；没有或填 auto 就返回空串"""
    path = CONFIG_DIR / f'{config_name}.json'
    if not path.is_file():
        return ''
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        serial = data.get('script', {}).get('device', {}).get('serial') or ''
    except Exception:
        return ''
    serial = str(serial).strip()
    return '' if serial.lower() in ('', 'auto') else serial


def _auto_serial() -> str:
    """填 auto 时取唯一一台在线设备"""
    out = subprocess.run(
        ['adb', 'devices'],
        capture_output=True,
        text=True,
        timeout=ADB_TIMEOUT_SECONDS,
    ).stdout
    online = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device':
            online.append(parts[0])
    if len(online) != 1:
        raise RuntimeError(
            f'无法自动确定设备（在线 {len(online)} 台），请在配置里填写模拟器地址'
        )
    return online[0]


def _ensure_adb_connected(serial: str) -> None:
    """截图前确保 adb 连着。

    网络类设备（ip:port）掉线后 adb 会把它标成 offline 或直接丢掉，
    这里先查状态，不通就重连一次，避免整次截图直接失败。
    """
    if ':' not in serial:
        return  # USB 设备不需要 connect
    try:
        state = subprocess.run(
            ['adb', '-s', serial, 'get-state'],
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT_SECONDS,
        )
        if state.returncode == 0 and state.stdout.strip() == 'device':
            return
    except Exception:
        pass
    try:
        subprocess.run(
            ['adb', 'connect', serial],
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT_SECONDS,
        )
    except Exception:
        pass


def capture(config_name: str, output_file: str) -> None:
    """adb 抓一帧并覆盖写入 output_file"""
    serial = _configured_serial(config_name) or _auto_serial()
    _ensure_adb_connected(serial)
    result = subprocess.run(
        ['adb', '-s', serial, 'exec-out', 'screencap', '-p'],
        capture_output=True,
        timeout=ADB_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.decode('utf-8', 'replace').strip()
        raise RuntimeError(detail or f'adb exited {result.returncode}')
    # 正常 PNG 至少几十 KB，太小说明没拿到画面
    if len(result.stdout) < 1024:
        raise RuntimeError('no screenshot data')

    output = Path(output_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result.stdout)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print('usage: screen_capture_worker.py <config_name> <output_file>', file=sys.stderr)
        return 2
    try:
        capture(argv[1], argv[2])
    except subprocess.TimeoutExpired:
        print(f'screenshot timeout after {ADB_TIMEOUT_SECONDS}s', file=sys.stderr)
        return 1
    except FileNotFoundError:
        print('adb not found', file=sys.stderr)
        return 1
    except Exception as e:
        print(f'{type(e).__name__}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
