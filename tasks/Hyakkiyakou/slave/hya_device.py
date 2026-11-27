import numpy as np
from datetime import datetime

from module.base.decorator import cached_property
from module.base.timer import Timer
from module.device.handle import Handle, WindowNode, handle_num2title, handle_title2num, is_handle_valid
from module.logger import logger
from module.base.utils import point2str
from module.exception import RequestHumanTakeover, GameStuckError
from tasks.base_task import BaseTask

from tasks.Hyakkiyakou.config import ScreenshotMethod, ControlMethod

def image_black(img) -> bool:
    for y, x in [(0, 0), (719, 1279), (719, 0), (0, 1279)]:
        if np.all(img[y, x] != 0):
            return False
    return True


class HyaDevice(BaseTask):
    """
    这个类主要是是优化截屏点击速度
    1. 使用特别的method
    2. 扔掉中间的冗余校验
    3. 考虑JIT加速
    我宣布世界上最好的 Linux 系统是 Windows
    """
    hya_screenshot_interval = Timer(0.2)  # 300ms
    hya_fs_check_timer = Timer(3 * 60)
    _nemu_ipc_failed = False

    @cached_property
    def _has_window_handle(self) -> bool:
        if getattr(self.device, 'root_node', None) and is_handle_valid(self.device.root_node.num):
            return True
        root_handle = self.device.config.script.device.handle
        if not root_handle:
            logger.warning('Hyakkiyakou: no window handle, using standard device methods')
            return False
        root_handle_title = ''
        root_handle_num = 0
        if root_handle == 'auto':
            logger.info('Handle is auto, searching for emulator window')
            window_list = Handle.all_windows()
            root_handle_title = Handle.auto_handle_title(window_list)
            if root_handle_title:
                root_handle_num = handle_title2num(root_handle_title)
        else:
            try:
                root_handle_num = int(root_handle)
            except ValueError:
                root_handle_num = handle_title2num(root_handle)
                root_handle_title = root_handle
        if not root_handle_num or not is_handle_valid(root_handle_num):
            logger.warning('Hyakkiyakou: invalid window handle, using standard device methods')
            return False
        if not root_handle_title:
            root_handle_title = handle_num2title(root_handle_num)
        self.device.root_handle_title = root_handle_title
        self.device.root_handle_num = root_handle_num
        self.device.root_node = WindowNode(name=root_handle_title, num=root_handle_num)
        Handle.handle_tree(root_handle_num, self.device.root_node)
        logger.info(f'root_node initialized: title={root_handle_title}, num={root_handle_num}')
        return True

    def _capture_fast_frame(self, screenshot: ScreenshotMethod):
        if screenshot == ScreenshotMethod.NEMU_IPC:
            if not self._nemu_ipc_failed:
                try:
                    return self.device.screenshot_nemu_ipc()
                except RequestHumanTakeover:
                    self._nemu_ipc_failed = True
                    logger.warning('Hyakkiyakou: Nemu IPC unavailable, using standard screenshot')
        elif self._has_window_handle:
            return self.device.screenshot_window_background()
        return None

    def fast_screenshot(self, screenshot: ScreenshotMethod):
        self.hya_screenshot_interval.wait()
        self.hya_screenshot_interval.reset()
        if self.hya_fs_check_timer.reached():
            raise GameStuckError('Hyakkiyakou round did not finish within three minutes')

        # The round timeout also covers periods without bean throws.
        self.device.stuck_timer.reset()
        fast_image = self._capture_fast_frame(screenshot)
        if fast_image is not None:
            self.device.image = fast_image
            self.device.image_frame_id = None
        else:
            self.device.screenshot()
        if image_black(self.device.image):
            logger.error('Screenshot image is black, try again')
            raise RequestHumanTakeover('Screenshot image is black, try again')
        if fast_image is not None and self.config.script.error.save_error:
            self.device.screenshot_deque.append({'time': datetime.now(), 'image': self.device.image})
        return self.device.image

    def fast_click(self, x: int, y: int, control_method: ControlMethod = ControlMethod.WINDOW_MESSAGE) -> None:
        if control_method == ControlMethod.MINITOUCH:
            self.device.click_minitouch(x=x, y=y)
        elif self._has_window_handle:
            self.device.click_window_message(x=x, y=y, fast=True)
        else:
            self.device.click(x, y, control_check=False, control_name='Hyakkiyakou bean throw')
            return
        logger.info(f'Click {point2str(x, y)} @ Hyakkiyakou bean throw')

    def set_fast_screenshot_interval(self, interval: float):
        """

        @param interval: ms
        @return:
        """
        self.hya_screenshot_interval = Timer(interval / 1000.)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    hd = HyaDevice(c, d)

    # def screenshot():
    #     global hd
    #     # hd.fast_screenshot()
    #     hd.fast_click(420, 370)
    #     hd.fast_click(750, 400)
    # execution_time = timeit.timeit(screenshot, number=50)
    # print(f"执行总的时间: {execution_time * 1000} ms")

    from tasks.Hyakkiyakou.config import ScreenshotMethod
    hd.fast_screenshot(screenshot=ScreenshotMethod.WINDOW_BACKGROUND)
