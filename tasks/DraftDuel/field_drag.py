"""协同对弈上阵：起点按住、连续移动、光柱落点松开。"""

import time

from module.base.utils import ensure_int, point2str
from module.device.method.minitouch import insert_swipe
from module.device.method.scrcpy import const
from module.logger import logger


def drag_to_field(device, p1, p2, name='draft_battle_field'):
    if device.handle_control_check(name) is False:
        return False
    p1, p2 = ensure_int(p1, p2)
    points = insert_swipe(p0=p1, p3=p2, speed=8, min_distance=2)
    if tuple(points[-1]) != tuple(p2):
        points.append(list(p2))
    hold, movement, release = 0.3, 0.5, 0.0
    step = movement / max(len(points) - 1, 1)
    method = device.config.script.device.control_method
    device._invalidate_image_batch_cache()
    start = time.perf_counter()
    if method == 'minitouch':
        builder = device.minitouch_builder
        try:
            builder.down(*p1).commit().wait(int(hold * 1000))
            device.minitouch_send()
            for point in points[1:]:
                builder.move(*point).commit().wait(max(1, round(step * 1000)))
            device.minitouch_send()
            builder.move(*p2).commit().wait(int(release * 1000))
            device.minitouch_send()
        finally:
            builder.clear()
            builder.up().commit()
            device.minitouch_send()
    elif method == 'scrcpy':
        device.scrcpy_ensure_running()
        with device._scrcpy_control_socket_lock:
            try:
                device._scrcpy_control.touch(*p1, const.ACTION_DOWN)
                device.sleep(hold)
                for point in points[1:]:
                    device._scrcpy_control.touch(*point, const.ACTION_MOVE)
                    device.sleep(step)
                device.sleep(release)
            finally:
                device._scrcpy_control.touch(*p2, const.ACTION_UP)
    else:
        # ADB 和窗口消息也通过已配置模拟器的触摸通道完成完整拖拽。
        # 不降级成点击或没有起点按住的 input swipe。
        touch = device.u2.touch
        try:
            touch.down(*p1)
            device.sleep(hold)
            for point in points[1:]:
                touch.move(*point)
                device.sleep(step)
            device.sleep(release)
        finally:
            touch.up(*p2)
    elapsed = time.perf_counter() - start
    logger.info(f'[{elapsed:.2f}s] DraftDuel hold-drag-release '
                f'{point2str(*p1)} -> {point2str(*p2)}')
    return True
