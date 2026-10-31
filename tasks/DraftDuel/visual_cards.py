# This Python file uses the following encoding: utf-8
"""协同对弈：大厅头像 ↔ 上阵手牌的图像匹配。

选完 5 张后，大厅顶部「我的阵容」一排头像和 picked_roles 一一对应；
上阵界面的手牌顺序会乱。这里用「手牌脸部 → 大厅头像」的反向多尺度匹配：
手牌脸部更清晰、尺寸更大，反向匹配比拿大厅小头像去搜手牌稳定得多。
全程只截图、不点卡。
"""
import numpy as np
import cv2

# 用户实测 ROI（1280x720，x, y, w, h）
AVATAR_BOXES = (
    (466, 22, 72, 53),
    (552, 24, 72, 53),
    (637, 23, 72, 53),
    (723, 22, 72, 53),
    (808, 22, 72, 53),
)
CARD_BOXES = (
    (293, 520, 61, 82),
    (406, 511, 61, 82),
    (519, 511, 61, 82),
    (633, 509, 61, 82),
    (749, 508, 61, 82),
)
# 在 ROI 内只取脸部，避开卡框、等级数字和自动上阵图标
AVATAR_FACE_INSET = (14, 3, 44, 44)
CARD_FACE_INSET = (8, 8, 45, 50)

_SCALES = tuple(np.arange(0.5, 1.81, 0.05))


def _face(image: np.ndarray, box: tuple[int, int, int, int],
          inset: tuple[int, int, int, int], scale: float) -> np.ndarray:
    x, y, _, _ = box
    ix, iy, iw, ih = inset
    x1 = int((x + ix) * scale)
    y1 = int((y + iy) * scale)
    x2 = int((x + ix + iw) * scale)
    y2 = int((y + iy + ih) * scale)
    crop = image[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
    if crop.size == 0:
        return np.empty((0, 0), dtype=np.uint8)
    return cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)


def lobby_faces(image: np.ndarray) -> list[np.ndarray]:
    scale = image.shape[0] / 720
    return [_face(image, box, AVATAR_FACE_INSET, scale) for box in AVATAR_BOXES]


def hand_faces(image: np.ndarray) -> list[np.ndarray]:
    scale = image.shape[0] / 720
    return [_face(image, box, CARD_FACE_INSET, scale) for box in CARD_BOXES]


def _reverse_score(card_face: np.ndarray, avatar_face: np.ndarray) -> float:
    """手牌脸部做模板，在大厅头像里搜；取多尺度最高分。"""
    best = 0.0
    for scale in _SCALES:
        template = cv2.resize(card_face, None, fx=float(scale), fy=float(scale))
        if template.shape[0] > avatar_face.shape[0] or \
                template.shape[1] > avatar_face.shape[1]:
            continue
        response = cv2.matchTemplate(avatar_face, template, cv2.TM_CCOEFF_NORMED)
        best = max(best, float(response.max()))
    return best


def match_hand(image: np.ndarray, faces: list[np.ndarray],
               threshold: float = 0.6) -> list[tuple[int, int, float]]:
    """返回 [(手牌卡位, 头像下标, 分数), ...]。

    先按分数一对一贪心；剩下一个卡位/头像时直接按排除法补上
    （自动上阵高亮那张经常匹配分低，只能靠排除）。
    """
    cards = hand_faces(image)
    scores = []
    for slot, card in enumerate(cards):
        if card.size == 0:
            continue
        for index, face in enumerate(faces):
            if face.size == 0:
                continue
            scores.append((_reverse_score(card, face), slot, index))
    scores.sort(reverse=True)

    score_map = {(slot, index): score for score, slot, index in scores}
    used_slots, used_faces, result = set(), set(), []
    for score, slot, index in scores:
        if score < threshold:
            break
        if slot in used_slots or index in used_faces:
            continue
        used_slots.add(slot)
        used_faces.add(index)
        result.append((slot, index, score))

    left_slots = [i for i in range(len(faces)) if i not in used_slots]
    left_faces = [i for i in range(len(faces)) if i not in used_faces]
    # 剩下的卡位/头像按一对一补全：分数可能低于阈值，但总比少识别一张好；
    # 调用方会把这些低分映射打印出来，便于人工核对。
    if left_slots and left_faces:
        remaining = sorted(
            ((score_map.get((slot, index), 0.0), slot, index)
             for slot in left_slots for index in left_faces),
            reverse=True,
        )
        used_slots, used_faces = set(), set()
        for score, slot, index in remaining:
            if slot in used_slots or index in used_faces:
                continue
            used_slots.add(slot)
            used_faces.add(index)
            result.append((slot, index, score))
    return sorted(result)
