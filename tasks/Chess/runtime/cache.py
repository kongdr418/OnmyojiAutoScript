"""Small, bounded caches and execution scopes used only by Chess."""

from functools import wraps
import time


def same_frame(method):
    """Keep one result per reader; never reuse it on a newer screenshot."""
    @wraps(method)
    def read(self):
        image = self.device.image
        key = (getattr(self.device, 'image_frame_id', None), id(image))
        cache = getattr(self, '_chess_reader_cache', None)
        if cache is None or cache[0] != key:
            cache = (key, {}, image)
            self._chess_reader_cache = cache
        if method.__name__ not in cache[1]:
            cache[1][method.__name__] = method(self)
        return cache[1][method.__name__]
    return read


def economy_scope(method):
    """Allow nested purchase/recovery helpers to honor the economy deadline."""
    @wraps(method)
    def run(self, *args, **kwargs):
        previous = getattr(self, '_economy_guard_round', None)
        self._economy_guard_round = previous if previous is not None else self._read_round_number()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._economy_guard_round = previous
    return run


def economy_action_allowed(self):
    expected_round = getattr(self, '_economy_guard_round', None)
    if expected_round is None:
        return True
    mode = self._read_chess_mode()
    if (self._read_round_number() != expected_round
            or mode not in ('备', '战')
            or self.appear(self.I_SELECT_GRIGRI)):
        return False
    if mode == '战':
        return True
    remaining = self._read_remaining_time()
    cache = getattr(self, '_detail_frame_cache', None)
    age = max(0, time.monotonic() - cache[2]) if cache and len(cache) > 2 else 0
    return remaining is None or remaining - age >= 12


def timed_action(method):
    """Report aggregate step latency for comparing actual Chess runs."""
    @wraps(method)
    def run(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return method(self, *args, **kwargs)
        finally:
            from module.logger import logger
            logger.debug(f'Chess timing: {method.__name__}={(time.perf_counter()-started)*1000:.1f}ms')
    return run
