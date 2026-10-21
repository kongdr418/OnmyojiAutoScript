# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import numpy as np
from contextvars import ContextVar
from math import ceil, hypot
from time import monotonic

from module.base.utils.utils import random_normal_distribution_int
from module.base.decorator import cached_property
from module.logger import logger


class RuleClick:
    _task_click_state = ContextVar('ruleclick_task_click_state', default=None)
    _recent_limit = 6
    _history_limit = 256
    _DENSITY_PROFILES = {
        None: (0.98, 0.02),
        'High': (1.0, 0.0),
        'More': (0.95, 0.05),
    }

    @classmethod
    def reset_task_points(cls):
        """每次任务启动清空各规则的近期落点和全局上一落点。"""
        RuleClick._task_click_state.set({
            'rules': {},
            'previous_point': None,
            'history': [],
        })

    def __init__(
        self,
        roi_front: tuple,
        roi_back: tuple,
        name: str = None,
        functional: bool = True,
        profile: str = None,
    ) -> None:
        """
        初始化
        :param roi_front:
        :param roi_back:
        """
        self.roi_front = roi_front
        self.roi_back = roi_back
        if name:
            self.name = name
        else:
            self.name = 'click'
        self.functional = functional
        if profile in ('', 'Default'):
            profile = None
        if isinstance(profile, str):
            profile = {'high': 'High', 'more': 'More'}.get(profile.lower(), profile)
        if profile not in self._DENSITY_PROFILES:
            raise ValueError('RuleClick profile must be High, More, or omitted')
        self.profile = profile

    @classmethod
    def task_click_history(cls) -> tuple:
        state = cls._task_click_state.get()
        if state is None:
            cls.reset_task_points()
            state = cls._task_click_state.get()
        return tuple(state['history'])

    def _record_task_click(self, point: tuple[int, int]) -> None:
        task_state = RuleClick._task_click_state.get()
        if task_state is None:
            self.reset_task_points()
            task_state = RuleClick._task_click_state.get()
        task_state['previous_point'] = point
        history = task_state['history']
        history.append((point[0], point[1], monotonic(), self.functional))
        del history[:-self._history_limit]

    def coord(self) -> tuple:
        """
        获取坐标，在 roi_front 内按近期落点自适应采样。
        :return:
        """
        return self._circle_normal_coord(self.roi_front)

    def coord_more(self) -> tuple:
        """
        在 roi_back 内按近期落点自适应采样。
        :return:
        """
        return self._circle_normal_coord(self.roi_back)

    def _circle_normal_coord(self, roi: tuple) -> tuple:
        """以近期落点收缩中心，在内椭圆与外角区间分层采样。"""
        x, y, width, height = roi
        if width <= 0 or height <= 0:
            raise ValueError(f'RuleClick roi must have positive size: {roi}')

        left, right = ceil(x), ceil(x + width)
        top, bottom = ceil(y), ceil(y + height)
        if left >= right or top >= bottom:
            raise ValueError(f'RuleClick roi contains no integer pixel: {roi}')
        task_state = RuleClick._task_click_state.get()
        if task_state is None:
            self.reset_task_points()
            task_state = RuleClick._task_click_state.get()
        rules = task_state['rules']
        # 动态创建的同名同区域规则也复用本任务的点击状态。
        key = (self.name, tuple(roi), self.profile)
        if key not in rules:
            rules[key] = {
                'anchor': self._uniform_density_point(roi, inner=True),
                'recent': [],
                'count': 0,
            }
        rule_state = rules[key]
        anchor_x, anchor_y = rule_state['anchor']
        recent = rule_state['recent']

        if recent:
            weights = np.arange(1, len(recent) + 1, dtype=float)
            recent_x = float(np.average([point[0] for point in recent], weights=weights))
            recent_y = float(np.average([point[1] for point in recent], weights=weights))
            # 保留初始习惯区域的约束，避免落点均值无界漂移。
            center_x = anchor_x * 0.35 + recent_x * 0.65
            center_y = anchor_y * 0.35 + recent_y * 0.65
        else:
            center_x, center_y = anchor_x, anchor_y

        previous = task_state['previous_point']
        if previous is None:
            direction_x, direction_y = 1.0, 0.0
            movement = hypot(width, height)
        else:
            direction_x = center_x - previous[0]
            direction_y = center_y - previous[1]
            movement = hypot(direction_x, direction_y)
        if movement < 1e-6:
            direction_x, direction_y = 1.0, 0.0
            movement = 0.0
        else:
            direction_x /= movement
            direction_y /= movement
        perpendicular_x, perpendicular_y = -direction_y, direction_x

        scale = max(1.0, min(width, height))
        # 点击次数越多越集中，但始终保留约6%目标尺度的离散度。
        shrink = max(0.45, 1.0 / np.sqrt(1.0 + 0.22 * rule_state['count']))
        movement_scale = min(movement, hypot(width, height) * 3)
        sigma_parallel = max(scale * 0.06, (scale * 0.24 + movement_scale * 0.035) * shrink)
        sigma_perpendicular = max(scale * 0.045, (scale * 0.14 + movement_scale * 0.018) * shrink)
        inner_probability, _ = self._DENSITY_PROFILES[self.profile]
        sample_inner = bool(np.random.random() < inner_probability)

        for _ in range(128):
            parallel_error = float(np.random.normal(0, sigma_parallel))
            perpendicular_error = float(np.random.normal(0, sigma_perpendicular))
            click_x = int(round(
                center_x + direction_x * parallel_error
                + perpendicular_x * perpendicular_error
            ))
            click_y = int(round(
                center_y + direction_y * parallel_error
                + perpendicular_y * perpendicular_error
            ))
            if (
                x <= click_x < x + width
                and y <= click_y < y + height
                and self._point_in_inner_ellipse(click_x, click_y, roi) == sample_inner
            ):
                recent.append((click_x, click_y))
                del recent[:-self._recent_limit]
                rule_state['count'] += 1
                self._record_task_click((click_x, click_y))
                return click_x, click_y

        # 收缩后的正态分布很少命中外角区，回退时直接在指定层内采样。
        click_x, click_y = self._uniform_density_point(roi, inner=sample_inner)
        recent.append((click_x, click_y))
        del recent[:-self._recent_limit]
        rule_state['count'] += 1
        self._record_task_click((click_x, click_y))
        return click_x, click_y

    @staticmethod
    def _point_in_inner_ellipse(click_x: float, click_y: float, roi: tuple) -> bool:
        x, y, width, height = roi
        center_x, center_y = x + width / 2, y + height / 2
        # 用像素中心判断，保证 1x1 等极小区域也有内椭圆落点。
        normalized_x = (click_x + 0.5 - center_x) / max(width / 2, 0.5)
        normalized_y = (click_y + 0.5 - center_y) / max(height / 2, 0.5)
        return normalized_x ** 2 + normalized_y ** 2 <= 1.0

    def _uniform_density_point(self, roi: tuple, inner: bool) -> tuple[int, int]:
        x, y, width, height = roi
        left, right = ceil(x), ceil(x + width)
        top, bottom = ceil(y), ceil(y + height)
        for _ in range(4096):
            click_x = int(random_normal_distribution_int(left, right))
            click_y = int(random_normal_distribution_int(top, bottom))
            if self._point_in_inner_ellipse(click_x, click_y, roi) == inner:
                return click_x, click_y
        # 极小区域可能根本没有外角像素，More 也不应因此失败。
        if not inner:
            return self._uniform_density_point(roi, inner=True)
        raise ValueError(f'RuleClick inner ellipse contains no integer pixel: {roi}')

    @property
    def center(self) -> tuple:
        """
        返回roi_front的中心坐标
        :return:
        """
        x, y, w, h = self.roi_front
        return x + w // 2, y + h // 2

    def move(self, x: int, y: int) -> None:
        """
        移动roi_front, 需要限幅x是0-1280, y是0-720
        :param x:
        :param y:
        :return:
        """
        x, y, w, h = self.roi_front
        x += x
        y += y
        if x <= 0:
            x = 0
        elif x >= 1280:
            x = 1280

        if y <= 0:
            y = 0
        elif y >= 720:
            y = 720

        self.roi_front = x, y, w, h

    def __repr__(self):
        return self.name
