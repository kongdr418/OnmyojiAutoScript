# This Python file uses the following encoding: utf-8
"""协同对弈上阵：按住卡片拖到落点。

轨迹和分通道实现参考 tasks/Chess/runtime/press_and_drag.py：
先按住，再走直线（起步/落点减速）轨迹，到点直接松开。
协同这边保留原来的 0.9s 长按/1.2s 移动/0.3s 释放，避免被游戏当成点击或滑动。
"""
import time

import numpy as np

import module.device.method.scrcpy.const as scrcpy_const
from module.base.utils import (
    ensure_int,
    ensure_time,
    point2str,
    random_rectangle_point,
)
from module.logger import logger


def drag_to_field(
    device,
    p1,
    p2,
    hold_duration=(0.85, 0.95),
    point_random=(-10, -10, 10, 10),
    swipe_duration=1.2,
    name='draft_battle_field',
):
    """在起点按住后拖到终点；只给协同上阵用。"""
    if device.handle_control_check(name) is False:
        return False
    p1, p2 = ensure_int(p1, p2)
    hold_duration = ensure_time(hold_duration)
    swipe_duration = ensure_time((swipe_duration * 0.92, swipe_duration * 1.08))
    action_log = f'DraftDuel hold-drag-release {point2str(*p1)} -> {point2str(*p2)}'
    method = device.config.script.device.control_method
    start = time.perf_counter()
    device._invalidate_image_batch_cache()

    if method == 'minitouch':
        _drag_minitouch(
            device, p1, p2,
            hold_duration=hold_duration,
            point_random=point_random,
            swipe_duration=swipe_duration,
        )
    elif method == 'uiautomator2':
        _drag_uiautomator2(
            device, p1, p2,
            hold_duration=hold_duration,
            point_random=point_random,
            swipe_duration=swipe_duration,
        )
    elif method == 'scrcpy':
        _drag_scrcpy(
            device, p1, p2,
            hold_duration=hold_duration,
            point_random=point_random,
            swipe_duration=swipe_duration,
        )
    else:
        logger.warning(
            f'Control method {method} cannot hold before moving; fallback to ADB swipe'
        )
        device.swipe_adb(
            p1, p2,
            duration=ensure_time(hold_duration + swipe_duration),
        )

    elapsed = time.perf_counter() - start
    logger.info(f'{device._format_action_duration(elapsed)}{action_log}')
    return True


def _randomized_points(p1, p2, point_random):
    p1 = np.array(p1) - random_rectangle_point(point_random)
    p2 = np.array(p2) - random_rectangle_point(point_random)
    return p1, p2


def _straight_curve(p1, p2, speed=20, min_distance=2):
    """直线轨迹；只保留最小加加速度的起步/落点减速，不做左右弧度。"""
    p1 = np.asarray(p1, dtype=float)
    p2 = np.asarray(p2, dtype=float)
    delta = p2 - p1
    distance = float(np.linalg.norm(delta))
    if distance <= 0:
        return [p1.astype(int).tolist(), p2.astype(int).tolist()]

    segments = max(int(distance / speed) + 1, 10)
    timeline = np.linspace(0, 1, segments + 1)
    progress_values = 10 * timeline ** 3 - 15 * timeline ** 4 + 6 * timeline ** 5
    points = []
    for progress in progress_values:
        point = (p1 + delta * progress).astype(int).tolist()
        if points and np.linalg.norm(np.subtract(point, points[-1])) < min_distance:
            continue
        points.append(point)

    end = p2.astype(int).tolist()
    if points[-1] != end:
        points.append(end)
    return points


def _drag_minitouch(device, p1, p2, hold_duration, point_random,
                    swipe_duration):
    p1, p2 = _randomized_points(p1, p2, point_random)
    points = _straight_curve(p1, p2, speed=20, min_distance=2)
    builder = device.minitouch_builder
    move_wait = max(1, int(swipe_duration * 1000 / max(len(points) - 1, 1)))

    builder.down(*points[0]).commit().wait(int(hold_duration * 1000))
    device.minitouch_send()
    for point in points[1:]:
        builder.move(*point).commit().wait(move_wait)
    device.minitouch_send()
    builder.move(*p2).commit()
    device.minitouch_send()
    builder.up().commit()
    device.minitouch_send()


def _drag_uiautomator2(device, p1, p2, hold_duration, point_random,
                       swipe_duration):
    p1, p2 = _randomized_points(p1, p2, point_random)
    points = _straight_curve(p1, p2, speed=35, min_distance=2)
    move_duration = swipe_duration / max(len(points) - 1, 1)
    path = [(int(points[0][0]), int(points[0][1]), hold_duration)]
    for point in points[1:]:
        path.append((int(point[0]), int(point[1]), move_duration))
    path.append((int(p2[0]), int(p2[1]), 0))
    device._drag_along(path)


def _drag_scrcpy(device, p1, p2, hold_duration, point_random,
                 swipe_duration):
    device.scrcpy_ensure_running()
    with device._scrcpy_control_socket_lock:
        p1, p2 = _randomized_points(p1, p2, point_random)
        points = _straight_curve(p1, p2, speed=6, min_distance=1)
        move_interval = swipe_duration / max(len(points) - 1, 1)
        device._scrcpy_control.touch(*p1, scrcpy_const.ACTION_DOWN)
        device.sleep(hold_duration)
        for point in points[1:]:
            device._scrcpy_control.touch(*point, scrcpy_const.ACTION_MOVE)
            device.sleep(move_interval)
        device._scrcpy_control.touch(*p2, scrcpy_const.ACTION_UP)
        device.sleep(0.05)
