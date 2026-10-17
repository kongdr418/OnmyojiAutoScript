# This Python file uses the following encoding: utf-8
"""模拟器画面截取接口。

一次只保留一张：每次截图覆盖同一个文件，路径是 log/screen/<配置名>.png。

放 log 的子目录而不是根目录，是因为 cleanup_logs() 只清理 log 根目录下的文件和
error 目录，其它子目录不递归（日收益的日汇总也是靠这一点长期保留）。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from module.logger import logger
from module.server.api_logger import ApiLoggingRoute

screen_app = APIRouter(
    prefix="/screen",
    tags=["screen"],
    route_class=ApiLoggingRoute,
)

# 仓库根目录（这个文件在 module/server/ 下）
BASE_DIR = Path(__file__).resolve().parents[2]
SCREEN_DIR = Path('./log/screen')
CONFIG_DIR = Path('./config')
# 正常截一张（连设备 + 读包名 + 抓帧）只要几秒。给 15 秒是为了模拟器不在线时
# 能尽快失败、把错误显示给用户，而不是让界面一直转圈。
CAPTURE_TIMEOUT_SECONDS = 15

# 连点时别同时连两次模拟器
_capture_lock = threading.Lock()

# 文件名里的危险字符，和日收益的日汇总用同一套规则（不按下划线截断）
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|]')


def screen_name(config_name: str) -> str:
    name = _UNSAFE_NAME.sub('_', str(config_name or '').strip())
    return name.replace('..', '_').strip(' .') or 'script'


def screen_path(config_name: str) -> Path:
    return SCREEN_DIR / f'{screen_name(config_name)}.png'


class ScreenInfoResponse(BaseModel):
    """画面元信息"""

    script_name: str
    # 有没有截过图
    has_image: bool = False
    # 上次截取时间 yyyy-MM-dd HH:mm:ss，没截过是空字符串
    captured_at: str = ''


def _require_config(config_name: str) -> None:
    if not (CONFIG_DIR / f'{config_name}.json').is_file():
        raise HTTPException(status_code=404, detail=f'Config not found: {config_name}')


def _screen_info(config_name: str) -> ScreenInfoResponse:
    path = screen_path(config_name)
    if not path.is_file():
        return ScreenInfoResponse(script_name=config_name)
    captured_at = datetime.fromtimestamp(path.stat().st_mtime)
    return ScreenInfoResponse(
        script_name=config_name,
        has_image=True,
        captured_at=captured_at.strftime('%Y-%m-%d %H:%M:%S'),
    )


def _friendly_capture_error(raw: str) -> str:
    """把 worker 的英文报错翻成人话，前端红框里直接显示它"""
    text = str(raw or '').strip()
    lowered = text.lower()
    if 'offline' in lowered:
        return '模拟器离线，请检查模拟器是否已启动、adb 连接是否正常'
    if 'connect' in lowered or 'refused' in lowered or 'unreachable' in lowered:
        return '连接不上模拟器，请检查模拟器地址与 adb 连接'
    if 'timeout' in lowered:
        return '截图超时，请检查模拟器状态'
    if 'requesthumantakeover' in lowered or 'initialize' in lowered:
        return '设备初始化失败，请检查配置里的模拟器地址和游戏包名'
    return f'截图失败：{text}' if text else '截图失败'


def _run_capture_worker(config_name: str, output: Path) -> None:
    """起个子进程截图，失败时把它的报错带回给调用方"""
    # 用 -m 启动，子进程的 sys.path 才会包含仓库根目录（按脚本路径启动就不会，
    # 那样 worker 里 import module.* 会直接 ModuleNotFoundError）
    env = dict(os.environ)
    existing = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = str(BASE_DIR) + (os.pathsep + existing if existing else '')
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'module.server.screen_capture_worker',
             config_name, str(output.resolve())],
            cwd=str(BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=CAPTURE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError('截图超时，请检查模拟器状态') from e

    if result.returncode != 0:
        lines = (result.stderr or result.stdout or '').strip().splitlines()
        raise RuntimeError(_friendly_capture_error(lines[-1] if lines else ''))


@screen_app.get('/{script_name}', response_model=ScreenInfoResponse)
async def screen_info(script_name: str):
    """画面元信息：有没有截过、什么时候截的"""
    _require_config(script_name)
    return _screen_info(script_name)


@screen_app.post('/{script_name}/capture', response_model=ScreenInfoResponse)
async def screen_capture(script_name: str):
    """截取该配置当前模拟器画面，覆盖上一张"""
    _require_config(script_name)

    if not _capture_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='Another capture is in progress')

    try:
        # 截图要连模拟器，几秒钟，丢到线程里别卡住事件循环（日志流是 SSE）
        try:
            await asyncio.to_thread(_run_capture_worker, script_name, screen_path(script_name))
        except Exception as e:
            logger.error(f'[screen] capture failed for {script_name}: {type(e).__name__}: {e}')
            raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        _capture_lock.release()

    logger.info(f'[screen] saved {screen_path(script_name)}')
    return _screen_info(script_name)


@screen_app.get('/{script_name}/image')
async def screen_image(script_name: str):
    """返回上次截取的画面"""
    _require_config(script_name)
    path = screen_path(script_name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail='No screen capture yet')
    return FileResponse(str(path), media_type='image/png')
