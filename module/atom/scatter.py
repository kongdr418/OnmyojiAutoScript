# This Python file uses the following encoding: utf-8
import math
import time

import numpy as np

from module.atom.click import RuleClick


class RuleScatter(RuleClick):
    """在多边形内按延迟生成的多个重心进行时序正态点击。"""

    _CLICKS_PER_FOCUS = 50
    _FUNCTIONAL_HISTORY_WEIGHT = 0.75
    _HISTORY_HALF_LIFE_SECONDS = 10 * 60
    _MIN_CLICK_RADIUS = 50.0
    _SHRINK_HALF_LIFE_CLICKS = 10
    _OUTSIDE_POLYGON_PROBABILITY = 0.01

    # 调度器为当前进程维护唯一任务上下文。generation 用于让所有
    # RuleScatter 在任务切换后延迟清空各自的倾向重心。
    _active_task_name: str | None = None
    _active_task_started_at: float | None = None
    _task_context_generation = 0

    def __init__(
        self,
        roi_front: tuple,
        roi_back: tuple,
        polygon: list[tuple[int, int]] | tuple[tuple[int, int], ...],
        focus_count: int,
        functional: bool = False,
        name: str = None,
    ) -> None:
        super().__init__(
            roi_front=roi_front,
            roi_back=roi_back,
            name=name,
            functional=functional,
        )
        if isinstance(focus_count, bool) or not isinstance(focus_count, int) \
                or focus_count <= 0:
            raise ValueError('focus_count must be a positive integer')
        self.polygon = self._normalize_polygon(polygon)
        self.focus_count = focus_count
        # 结算点击会浅拷贝规则，可变状态确保拷贝仍累计同一任务的点击。
        self._scatter_state = {
            'focuses': [],
            'focus_click_counts': [],
            'click_count': 0,
            'max_radius': None,
        }

    @classmethod
    def begin_task(cls, task_name: str) -> None:
        """开始新的调度任务并重新计时。"""
        cls._task_context_generation += 1
        cls._active_task_name = str(task_name)
        cls._active_task_started_at = time.monotonic()

    def reset_click_focuses(self) -> None:
        """进入专属任务时清空重心，首次点击时再生成第一个。"""
        self._scatter_state.clear()
        self._scatter_state.update(
            focuses=[],
            focus_click_counts=[],
            click_count=0,
            max_radius=None,
        )

    @classmethod
    def end_task(cls, task_name: str | None = None) -> None:
        """结束当前调度任务，清除长任务点击倾向。"""
        if (
            task_name is not None
            and cls._active_task_name is not None
            and str(task_name) != cls._active_task_name
        ):
            return
        cls._task_context_generation += 1
        cls._active_task_name = None
        cls._active_task_started_at = None

    def coord(self) -> tuple:
        return self._random_normal_point()

    def coord_more(self) -> tuple:
        return self.coord()

    @property
    def center(self) -> tuple:
        self._ensure_click_focuses()
        center_x, center_y = self._scatter_state['focuses'][0]
        return int(round(center_x)), int(round(center_y))

    def move(self, x: int, y: int) -> None:
        origin_x, origin_y, width, height = self.roi_front
        target_x = min(1280, max(0, origin_x + x))
        target_y = min(720, max(0, origin_y + y))
        dx, dy = target_x - origin_x, target_y - origin_y
        self.roi_front = target_x, target_y, width, height
        self.polygon = tuple((px + dx, py + dy) for px, py in self.polygon)
        self._scatter_state['focuses'][:] = [
            (focus_x + dx, focus_y + dy)
            for focus_x, focus_y in self._scatter_state['focuses']
        ]

    @staticmethod
    def _normalize_polygon(polygon):
        try:
            points = tuple((int(round(x)), int(round(y))) for x, y in polygon)
        except (TypeError, ValueError) as exc:
            raise ValueError('polygon must contain numeric (x, y) points') from exc
        if len(points) < 3 or len(set(points)) < 3:
            raise ValueError('polygon must contain at least three distinct points')
        return points

    def _uniform_polygon_point(self) -> tuple[float, float]:
        xs, ys = zip(*self.polygon)
        left, right = float(min(xs)), float(max(xs))
        top, bottom = float(min(ys)), float(max(ys))
        for _ in range(4096):
            point = (
                float(np.random.uniform(left, right)),
                float(np.random.uniform(top, bottom)),
            )
            if self._point_in_polygon(*point):
                return point
        raise ValueError('RuleScatter polygon contains no sampleable point')

    def _history_focus_point(self) -> tuple[float, float] | None:
        """按功能权重选组，再按时间衰减从框内历史落点中选重心。"""
        history = [
            item for item in self.task_click_history()
            if self._point_in_polygon(item[0], item[1])
        ]
        if not history:
            return None
        functional = [item for item in history if item[3]]
        non_functional = [item for item in history if not item[3]]
        if functional and non_functional:
            source = functional if np.random.random() < \
                self._FUNCTIONAL_HISTORY_WEIGHT else non_functional
        else:
            source = functional or non_functional

        now = time.monotonic()
        decay = math.log(2) / self._HISTORY_HALF_LIFE_SECONDS
        weights = np.asarray([
            math.exp(-max(0.0, now - item[2]) * decay)
            for item in source
        ], dtype=float)
        weights /= weights.sum()
        selected = source[int(np.random.choice(len(source), p=weights))]
        return float(selected[0]), float(selected[1])

    def _generate_click_focus(self) -> tuple[float, float]:
        reference = self._history_focus_point()
        if reference is None:
            return self._uniform_polygon_point()
        focuses = self._scatter_state['focuses']
        if not focuses:
            return reference

        previous_x, previous_y = focuses[-1]
        target_x, target_y = reference
        progress = len(focuses) / max(1, self.focus_count - 1)
        curve = math.sin(math.pi * min(1.0, progress))
        width = max(x for x, _ in self.polygon) - min(x for x, _ in self.polygon)
        candidate = (
            previous_x * 0.35 + target_x * 0.65 - width * 0.08 * curve,
            previous_y * 0.35 + target_y * 0.65,
        )
        return candidate if self._point_in_polygon(*candidate) else reference

    def _ensure_click_focuses(self) -> None:
        state = self._scatter_state
        desired = min(
            self.focus_count,
            1 + state['click_count'] // self._CLICKS_PER_FOCUS,
        )
        while len(state['focuses']) < desired:
            focus = self._generate_click_focus()
            state['focuses'].append(focus)
            state['focus_click_counts'].append(0)
            if state['max_radius'] is None:
                state['max_radius'] = max(
                    math.hypot(focus[0] - x, focus[1] - y)
                    for x, y in self.polygon
                )

    def _select_focus_index(self) -> int:
        weights = np.arange(1, len(self._scatter_state['focuses']) + 1, dtype=float)
        weights /= weights.sum()
        return int(np.random.choice(len(self._scatter_state['focuses']), p=weights))

    def _random_normal_point(self) -> tuple:
        self._ensure_click_focuses()
        state = self._scatter_state
        index = self._select_focus_index()
        focus_x, focus_y = state['focuses'][index]
        local_count = state['focus_click_counts'][index]
        # 框外的上一点会把小型 Scatter 的临时中心拉出可点区域。
        history = [
            item for item in self.task_click_history()
            if self._point_in_polygon(item[0], item[1])
        ]
        previous = history[-1][:2] if history else (focus_x, focus_y)
        phase = min(1.0, (local_count + 1) / self._CLICKS_PER_FOCUS)
        convergence = 0.22 + 0.58 * phase
        center_x = previous[0] + (focus_x - previous[0]) * convergence
        center_y = previous[1] + (focus_y - previous[1]) * convergence
        direction_x, direction_y = focus_x - previous[0], focus_y - previous[1]
        distance = math.hypot(direction_x, direction_y)
        if distance < 1e-6:
            direction_x, direction_y = 1.0, 0.0
        else:
            direction_x /= distance
            direction_y /= distance
        perpendicular_x, perpendicular_y = -direction_y, direction_x

        # 按整个规则的点击次数收缩，新重心加入时不重新放大范围。
        shrink = 0.5 ** (state['click_count'] / self._SHRINK_HALF_LIFE_CLICKS)
        radius = max(self._MIN_CLICK_RADIUS, state['max_radius'] * shrink)
        allow_outside_polygon = (
            np.random.random() < self._OUTSIDE_POLYGON_PROBABILITY
        )
        for _ in range(192):
            parallel = float(np.random.normal(0, max(1.0, radius / 3)))
            perpendicular = float(np.random.normal(0, max(1.0, radius / 5)))
            click_x = int(round(
                center_x + direction_x * parallel
                + perpendicular_x * perpendicular
            ))
            click_y = int(round(
                center_y + direction_y * parallel
                + perpendicular_y * perpendicular
            ))
            inside_circle = math.hypot(
                click_x - center_x, click_y - center_y
            ) <= radius
            inside_polygon = self._point_in_polygon(click_x, click_y)
            outside_in_circle = (
                allow_outside_polygon
                and 0 <= click_x < 1280
                and 0 <= click_y < 720
                and inside_circle
            )
            if inside_circle and (inside_polygon or outside_in_circle):
                state['focus_click_counts'][index] += 1
                state['click_count'] += 1
                self._record_task_click((click_x, click_y))
                return click_x, click_y

        fallback_x = int(round(center_x))
        fallback_y = int(round(center_y))
        if self._point_in_polygon(fallback_x, fallback_y):
            point = fallback_x, fallback_y
        else:
            point = self._nearest_integer_point(fallback_x, fallback_y)
        state['focus_click_counts'][index] += 1
        state['click_count'] += 1
        self._record_task_click(point)
        return point

    def _nearest_integer_point(self, center_x: float, center_y: float) -> tuple[int, int]:
        # 临时中心即使在框外，搜索半径也必须能覆盖整个多边形。
        limit = int(math.ceil(max(
            math.hypot(center_x - x, center_y - y)
            for x, y in self.polygon
        ))) + 1
        for distance in range(1, limit + 1):
            for offset in range(-distance, distance + 1):
                candidates = (
                    (center_x + offset, center_y - distance),
                    (center_x + offset, center_y + distance),
                    (center_x - distance, center_y + offset),
                    (center_x + distance, center_y + offset),
                )
                for click_x, click_y in candidates:
                    if self._point_in_polygon(click_x, click_y):
                        return click_x, click_y
        raise ValueError('RuleScatter polygon contains no integer point')

    def _point_in_polygon(self, x: float, y: float) -> bool:
        inside = False
        previous_x, previous_y = self.polygon[-1]
        for current_x, current_y in self.polygon:
            if (current_y > y) != (previous_y > y):
                intersect_x = (
                    (previous_x - current_x) * (y - current_y)
                    / (previous_y - current_y)
                    + current_x
                )
                if x < intersect_x:
                    inside = not inside
            previous_x, previous_y = current_x, current_y
        return inside
