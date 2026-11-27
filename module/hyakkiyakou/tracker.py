"""Run the full-screen model and feed tracks to the OAS bean-throwing agent."""
from datetime import timedelta
from pathlib import Path
from time import monotonic

import cv2
import numpy as np

from .labels import INITIAL_IDS, MODEL_CLASS_IDS, id2label


MODEL_PATH = Path(__file__).resolve().parents[2] / 'tasks/Hyakkiyakou/model/tracker.onnx'


class Tracker:
    def __init__(self, args=None):
        args = args or {}
        self.conf_threshold = float(args.get('conf_threshold', .6))
        self.iou_threshold = float(args.get('iou_threshold', .7))
        self.detect_time = timedelta()
        self.track_time = timedelta()
        self._session = None
        self._tensor = np.full((1, 3, 736, 1280), np.float32(114 / 255), dtype=np.float32)
        self.clear_tracks()

    def prepare(self):
        if self._session is None:
            from onnxruntime import InferenceSession, SessionOptions

            options = SessionOptions()
            options.intra_op_num_threads = 4
            self._session = InferenceSession(str(MODEL_PATH), sess_options=options,
                                             providers=['CPUExecutionProvider'])
            self._input = self._session.get_inputs()[0].name

    def clear_tracks(self):
        self.current_tracks = []
        self._tracks = {}
        self.id_cnt = 0
        self._last_frame_at = None

    @staticmethod
    def _iou(a, b):
        left = max(a[0] - a[2] / 2, b[0] - b[2] / 2)
        right = min(a[0] + a[2] / 2, b[0] + b[2] / 2)
        top = max(a[1] - a[3] / 2, b[1] - b[3] / 2)
        bottom = min(a[1] + a[3] / 2, b[1] + b[3] / 2)
        overlap = max(0, right - left) * max(0, bottom - top)
        return overlap / (a[2] * a[3] + b[2] * b[3] - overlap)

    def detect(self, image, include_initial=False):
        self.prepare()
        started = monotonic()
        if image.shape != (720, 1280, 3):
            raise ValueError('百鬼夜行需要 1280×720 横屏 RGB 画面')
        # Letterbox the RGB frame to the stride-aligned height without scaling the game coordinates.
        np.multiply(image.transpose(2, 0, 1), np.float32(1 / 255), out=self._tensor[0, :, 8:728])
        raw = self._session.run(None, {self._input: self._tensor})[0][0]
        scores = raw[4:]
        classes = scores.argmax(axis=0)
        confidence = scores[classes, np.arange(classes.size)]
        selected = confidence >= self.conf_threshold
        rows = zip(*raw[:4, selected], confidence[selected], classes[selected])
        detections = []
        for x, y, width, height, confidence, model_class in rows:
            label = MODEL_CLASS_IDS[int(model_class)]
            if label is None or (label in INITIAL_IDS and not include_initial):
                continue
            x, y, width, height = float(x), float(y) - 8, float(width), float(height)
            left, right = max(0, x - width / 2), min(1280, x + width / 2)
            top, bottom = max(0, y - height / 2), min(720, y + height / 2)
            if right <= left or bottom <= top:
                continue
            box = ((left + right) / 2, (top + bottom) / 2, right - left, bottom - top)
            detections.append((label, float(confidence), box))
        kept = []
        for detection in sorted(detections, key=lambda item: item[1], reverse=True):
            label, _, box = detection
            if not any(label == other[0] and self._iou(box, other[2]) > self.iou_threshold for other in kept):
                kept.append(detection)
        self.detect_time = timedelta(seconds=monotonic() - started)
        return kept

    def __call__(self, image, response=None):
        frame_at = monotonic()
        return self.update(self.detect(image), response, frame_at)

    def update(self, detections, response=None, now=None):
        started = monotonic()
        now = started if now is None else now
        elapsed = max(0.001, now - self._last_frame_at) if self._last_frame_at is not None else 0.3
        self._last_frame_at = now
        candidates = []
        for index, (label, _, box) in enumerate(detections):
            for track_id, track in self._tracks.items():
                if track['label'] != label or now - track['seen'] > .7:
                    continue
                predicted_x = track['box'][0] + track['velocity'] * (now - track['seen']) * 1000
                distance = np.hypot(box[0] - predicted_x, box[1] - track['box'][1])
                if distance < max(100, box[2] * 1.5):
                    candidates.append((distance, index, track_id))

        assignments = {}
        used = set()
        for _, index, track_id in sorted(candidates):
            if index not in assignments and track_id not in used:
                assignments[index] = track_id
                used.add(track_id)

        observed = set()
        for index, (label, confidence, box) in enumerate(detections):
            track_id = assignments.get(index)
            if track_id is None:
                track_id = self.id_cnt
                self.id_cnt += 1
                velocity = -.1
            else:
                previous = self._tracks[track_id]
                delta_ms = max(elapsed, now - previous['seen']) * 1000
                velocity = .6 * previous['velocity'] + .4 * (box[0] - previous['box'][0]) / delta_ms
            self._tracks[track_id] = dict(label=label, confidence=confidence, box=box, velocity=velocity, seen=now)
            observed.add(track_id)

        result = []
        for track_id, track in list(self._tracks.items()):
            x, y, width, height = track['box']
            if track_id not in observed:
                age = now - track['seen']
                x += track['velocity'] * age * 1000
                hit = response is not None and response[2] and abs(float(response[0]) - x) < max(100, width)
                left, right = max(0, x - width / 2), min(1280, x + width / 2)
                if hit or age > .35 or right <= left:
                    del self._tracks[track_id]
                    continue
                x, width = (left + right) / 2, right - left
            result.append((track_id, track['label'], track['confidence'], int(x), int(y), int(width), int(height), track['velocity']))
        self.current_tracks = result
        self.track_time = timedelta(seconds=monotonic() - started)
        return result


def draw_tracks(image, tracks):
    """Draw the debug overlay on a BGR copy for OpenCV display."""
    rendered = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    for track_id, label, confidence, x, y, width, height, _ in tracks:
        left, top = int(x - width / 2), int(y - height / 2)
        right, bottom = int(x + width / 2), int(y + height / 2)
        color = (64, 240, 160)
        cv2.rectangle(rendered, (left, top), (right, bottom), color, 1)
        caption = f'{track_id} {id2label(label)} {confidence:.2f}'
        cv2.putText(rendered, caption, (max(left, 0), max(top - 4, 16)),
                    cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1)
    return rendered
