# This Python file uses the following encoding: utf-8
"""模拟器画面截取 worker。

单独起一个进程来截图，而不是在 Web 服务进程里直接 new Device：标注工具也是
这么做的（见 tool_capture_worker.py），能把设备连接、卡死检测这些东西隔离开，
截完就随进程一起回收，不留在服务进程里。

用法：python screen_capture_worker.py <配置名> <输出文件>
进程退出码 0 表示成功，其它表示失败（错误写到 stdout/stderr）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2

from module.config.config import Config
from module.device.device import Device
from module.logger import logger


def capture(config_name: str, output_file: str) -> None:
    """截一帧并覆盖写入 output_file"""
    config = Config(config_name=config_name)
    device = Device(config=config)
    # 只截一张，不需要卡死检测
    device.disable_stuck_detection()
    try:
        frame = device.screenshot()
        if frame is None:
            raise RuntimeError('screenshot returned nothing')
        output = Path(output_file)
        output.parent.mkdir(parents=True, exist_ok=True)
        # device.screenshot() 给的是 RGB，cv2.imwrite 要 BGR
        if not cv2.imwrite(str(output), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)):
            raise RuntimeError(f'write {output} failed')
        logger.info(f'[screen] captured {output}')
    finally:
        try:
            device.release_during_wait()
        except Exception as e:
            # 和设备侧清理失败有关，别静默吞掉
            logger.warning(f'[screen] release device failed: {type(e).__name__}: {e}')


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print('usage: screen_capture_worker.py <config_name> <output_file>', file=sys.stderr)
        return 2
    try:
        capture(argv[1], argv[2])
    except Exception as e:
        print(f'{type(e).__name__}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
