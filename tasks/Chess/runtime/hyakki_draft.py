"""百鬼试行投掷：黄色文字框、对应锁、四秒计时及场上御魂定位。"""

from pathlib import Path
from dataclasses import dataclass
import random
import time

import cv2
import numpy as np
from PIL import Image
from module.base.decorator import cached_property
from module.logger import logger


@dataclass
class HyakkiTurn:
    key: tuple
    seen_lock: bool = False
    unlocked_at: float | None = None
    absent_frames: int = 0
    throws: int = 0

    def ready(self, score, now):
        if self.throws >= 2:
            return False
        if score is not None and score >= .8:
            self.seen_lock = True
            self.unlocked_at = None
            self.absent_frames = 0
        elif score is not None and score < .55 and self.seen_lock:
            if self.unlocked_at is None:
                self.unlocked_at = now
            self.absent_frames += 1
        else:
            self.unlocked_at = None
            self.absent_frames = 0
        return (self.unlocked_at is not None and self.absent_frames >= 2
                and now - self.unlocked_at >= 4)


def player_name_box(rule, image):
    """只按文字框内的黄色像素定位本人，不依赖 OCR 识别的昵称。"""
    results = rule.detect_and_ocr(image)
    candidates = []
    for result in results:
        text = ''.join(str(result.ocr_text).split())
        points = np.asarray(result.box)
        left, top = points.min(axis=0).astype(int)
        right, bottom = points.max(axis=0).astype(int)
        box = tuple(map(int, (left + rule.roi[0], top + rule.roi[1], right-left, bottom-top)))
        x, y, w, h = box
        patch = image[y:y+h, x:x+w]
        if patch.size == 0:
            continue
        hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
        yellow = float(np.mean((hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 40)
                               & (hsv[:, :, 1] >= 170) & (hsv[:, :, 2] >= 120)))
        # 生命值也带浅金色；要求较高饱和度及像素占比，排除数字行。
        if yellow >= .08:
            candidates.append((text, box))
    return candidates[0] if len(candidates) == 1 else None


def read_rgb(path):
    with Image.open(path) as image:
        return np.array(image.convert('RGB'))


def own_lock_score(image, name_box, rule, front):
    """只检测本人昵称下方的锁；None 表示选区不完整，不能判定消失。"""
    _, name_y, _, name_h = name_box
    rx, ry, rw, rh = rule.roi_back
    left, right = max(0, rx), min(image.shape[1], rx + rw)
    top = max(ry, name_y + name_h - 2)
    bottom = min(image.shape[0], ry + rh, name_y + name_h + 40)
    region = image[top:bottom, left:right]
    if region.shape[0] < front.shape[0] or region.shape[1] < front.shape[1]:
        return None
    return float(cv2.minMaxLoc(cv2.matchTemplate(
        region, front, cv2.TM_CCOEFF_NORMED,
    ))[1])


def load_soul_fronts(folder):
    return {path.stem.removeprefix('sou_'): read_rgb(path)
            for path in sorted(Path(folder).glob('sou_*.png'))}


def find_souls(image, roi, fronts, threshold=.85):
    """搜索用户圈定的范围，同一图标只保留最高分且种类明确的匹配。"""
    x, y, w, h = roi
    region = image[y:y+h, x:x+w]
    candidates = []
    for name, front in fronts.items():
        fh, fw = front.shape[:2]
        if region.shape[0] < fh or region.shape[1] < fw:
            continue
        scores = cv2.matchTemplate(region, front, cv2.TM_CCOEFF_NORMED)
        # 每种至多四个候选，抑制同一图标附近的重复峰值。
        for _ in range(4):
            _, score, _, (px, py) = cv2.minMaxLoc(scores)
            if not np.isfinite(score) or score < threshold:
                break
            candidates.append({'name': name, 'score': float(score),
                               'center': (x + px + fw // 2, y + py + fh // 2)})
            scores[max(0, py-20):py+21, max(0, px-20):px+21] = -1
    found = []
    for candidate in sorted(candidates, key=lambda item: item['score'], reverse=True):
        cx, cy = candidate['center']
        neighbours = [item for item in candidates if item is not candidate
                      and abs(item['center'][0]-cx) < 25
                      and abs(item['center'][1]-cy) < 25]
        if any(item['name'] != candidate['name'] and
               candidate['score'] - item['score'] < .05 for item in neighbours):
            continue
        if any(abs(item['center'][0]-cx) < 25 and abs(item['center'][1]-cy) < 25
               for item in found):
            continue
        found.append(candidate)
    return found


def throw_point(previous, latest, elapsed, latency, roi):
    """按最近两帧左移速度补偿识别耗时，落点必须在用户圈定范围内。"""
    x, y = latest['center']
    dx = x - previous['center'][0]
    dy = y - previous['center'][1]
    offset = 0
    if .03 <= elapsed <= 2 and -150 <= dx <= 0 and abs(dy) <= 45:
        offset = max(-35, min(0, round(dx / elapsed * min(latency + .05, .5))))
    point = (x + offset, y + 150)
    rx, ry, rw, rh = roi
    return point if rx <= point[0] < rx+rw and ry <= point[1] < ry+rh else None


class ChessHyakkiDraftMixin:
    @cached_property
    def hyakki_soul_fronts(self):
        return load_soul_fronts(Path(__file__).resolve().parents[1] / 'resource/hyakki_soul')

    @cached_property
    def hyakki_wait_front(self):
        return read_rgb(self.I_HYAKKI_WAIT.file)

    def _hyakki_own_lock(self):
        player = player_name_box(self.O_NAME_RANGE, self.device.image)
        if player is None:
            return None
        identity = (player[0], player[1][1])
        if getattr(self, '_hyakki_player_row', None) != identity:
            self._hyakki_player_row = identity
            logger.info(f'Chess Hyakki own player: name={player[0]}, box={player[1]}')
        return own_lock_score(self.device.image, player[1], self.I_HYAKKI_WAIT,
                              self.hyakki_wait_front)

    def _hyakki_needed_souls(self):
        needed = set()
        for name, config in self.get_lineup_strategy()['shikigami'].items():
            _, first, second = self._shikigami_attributes(name)
            needed.update(soul for soul in config.get('preferred_souls', ())
                          if soul not in (first, second))
        return needed

    def _handle_hyakki_draft(self):
        config = getattr(getattr(self.config, 'chess', None), 'chess_config', None)
        if not getattr(config, 'hyakki_draft_enabled', True):
            return
        key = (self.chess_state.game_number, self._current_round_no)
        state = getattr(self, '_hyakki_turn', None)
        if state is None or state.key != key:
            state = self._hyakki_turn = HyakkiTurn(key)
            logger.info(f'Chess Hyakki trial: waiting for own lock, round={key[1]}')
        if state.throws >= 2:
            return
        score = self._hyakki_own_lock()
        previous_unlock = state.unlocked_at
        ready = state.ready(score, time.monotonic())
        if previous_unlock is None and state.unlocked_at is not None:
            logger.info('Chess Hyakki own lock disappeared: wait 4 seconds')
        if not ready:
            return
        roi = self.C_HYAKKI_SOUL_SHIKI.roi_back
        first_at = time.monotonic()
        candidates = find_souls(self.device.image, roi, self.hyakki_soul_fronts)
        # 只选择可投掷范围内的目标，边缘目标等待进入范围。
        candidates = [item for item in candidates if throw_point(item, item, 0, 0, roi)]
        if not candidates:
            return
        needed = self._hyakki_needed_souls()
        preferred = [item for item in candidates if item['name'] in needed]
        chosen = random.choice(preferred or candidates)
        # 四秒等待结束后才定位；投掷前再取一帧，避免沿用移动前的位置。
        self.screenshot()
        captured_at = time.monotonic()
        if self._read_chess_mode() != '鬼':
            return
        if not state.ready(self._hyakki_own_lock(), time.monotonic()):
            return
        latest = [item for item in find_souls(self.device.image, roi, self.hyakki_soul_fronts)
                  if item['name'] == chosen['name']
                  and abs(item['center'][0]-chosen['center'][0]) <= 150
                  and abs(item['center'][1]-chosen['center'][1]) <= 45]
        if not latest:
            return
        target = min(latest, key=lambda item: abs(item['center'][0]-chosen['center'][0]))
        point = throw_point(chosen, target, captured_at-first_at,
                            time.monotonic()-captured_at, roi)
        if point is None:
            return
        state.throws += 1
        self.device.click(x=point[0], y=point[1], control_name='CHESS_HYAKKI_THROW')
        logger.info(f'Chess Hyakki throw attempted: soul={target["name"]}, '
                    f'preferred={bool(preferred)}, score={target["score"]:.3f}, '
                    f'icon={target["center"]}, point={point}, round={key[1]}, '
                    f'attempt={state.throws}/2')
