"""Chess-only watchdog: screenshots, recognition and input all count as activity."""

import ctypes
import sys
import threading
import time

from module.exception import GameStuckError
from module.logger import logger


class ChessInactiveError(GameStuckError):
    def __init__(self):
        super().__init__('Wait too long')


class ChessActivityWatchdog:
    def __init__(self, timeout=300):
        self.timeout = timeout
        self.last_activity = time.monotonic()
        self.stop_event = threading.Event()

    @staticmethod
    def is_activity(frame):
        path = frame.f_code.co_filename.replace('\\', '/').lower()
        name = frame.f_code.co_name
        if '/module/device/' in path:
            return name.startswith(('screenshot', 'click', 'long_click', 'swipe', 'drag'))
        return (
            '/module/ocr/' in path
            or '/module/image/' in path
            or path.endswith(('/module/atom/ocr.py', '/module/atom/image.py', '/module/atom/frame.py'))
            or path.endswith(('/tasks/chess/runtime/recognition.py', '/tasks/chess/runtime/frame_cards.py'))
        )

    def _profile(self, frame, event, arg):
        if event == 'return' and self.is_activity(frame):
            self.last_activity = time.monotonic()
        if self.previous_profile is not None:
            self.previous_profile(frame, event, arg)

    def _raise_timeout(self):
        # Deliver the ordinary task error to the task's own thread, so the
        # existing error screenshot/recovery path can handle it.
        api = ctypes.pythonapi.PyThreadState_SetAsyncExc
        count = api(ctypes.c_ulong(self.owner_thread), ctypes.py_object(ChessInactiveError))
        if count > 1:
            api(ctypes.c_ulong(self.owner_thread), ctypes.c_void_p(0))
            logger.error('Chess inactivity watchdog could not identify a unique task thread')

    def _watch(self):
        while not self.stop_event.wait(1):
            if time.monotonic() - self.last_activity >= self.timeout:
                logger.error(f'Wait too long: Chess has no screenshot, recognition or input activity for {self.timeout}s')
                self._raise_timeout()
                return

    def __enter__(self):
        self.owner_thread = threading.get_ident()
        self.previous_profile = sys.getprofile()
        self.last_activity = time.monotonic()
        self.thread = threading.Thread(target=self._watch, name='chess_activity_watchdog', daemon=True)
        sys.setprofile(self._profile)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        sys.setprofile(self.previous_profile)
        self.thread.join(timeout=2)
