# This Python file uses the following encoding: utf-8
"""协同对弈专用的 PP-OCRv6-small 识别封装。

只加载识别模型（不加载检测模型），因为协同对弈的规则都是 Single 模式。
模型文件不进 git：缺失时首次运行会从 Paddle 官方地址下载并校验 SHA256。
预处理与 CTC 解码逻辑来自 MIT 许可的 ppocrv6_onnx
(https://github.com/Kansallisarkisto-AI/ppocrv6_onnx)，这里只保留需要的最小部分，
避免给整个项目引入新的 pip 依赖。
"""
import hashlib
import math
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

MODEL_DIR = Path(__file__).resolve().parent / "model"
MODEL_PATH = MODEL_DIR / "PP-OCRv6_small_rec.onnx"
DICT_PATH = MODEL_DIR / "rec_small_dict.txt"

REC_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/"
    "paddle3.0.0/tmp/PP-OCRv6_small_rec_0515_onnx.tar"
)
REC_SHA256 = "0982f0ed51443ff4aa497a46fa9f9674024c02af6cc80ad2d0e793ffbe68ea01"


def _ensure_model(path: Path = MODEL_PATH) -> Path:
    """模型缺失时下载并校验，已存在则直接返回。"""
    if path.exists() and path.stat().st_size > 0:
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "rec.tar"
        with urllib.request.urlopen(REC_URL, timeout=60) as resp, open(tar_path, "wb") as f:
            shutil.copyfileobj(resp, f)

        with tarfile.open(tar_path) as tar:
            member = next(
                (m for m in tar.getmembers() if m.name.endswith("inference.onnx")),
                None,
            )
            if member is None:
                raise RuntimeError("v6 rec model not found in archive")
            src = tar.extractfile(member)
            if src is None:
                raise RuntimeError("v6 rec model is not a regular file")
            tmp_model = Path(tmp) / "model.onnx"
            with open(tmp_model, "wb") as f:
                shutil.copyfileobj(src, f)

        digest = hashlib.sha256(tmp_model.read_bytes()).hexdigest()
        if digest != REC_SHA256:
            raise RuntimeError(f"v6 rec model sha256 mismatch: {digest}")
        shutil.move(str(tmp_model), str(path))
    return path


class _RecPreProcess:
    """高度固定 48、宽度按比例动态计算并归一化到 [-1, 1]。"""

    def __init__(self, rec_image_shape=(3, 48, 320), max_width: int = 3200):
        self._c, self._h, self._w_min = rec_image_shape
        self._max_w = int(max_width)
        _x = np.arange(256, dtype=np.float32) * (1.0 / 255.0)
        self._lut = (_x - 0.5) / 0.5

    def single(self, img: np.ndarray) -> np.ndarray:
        h = self._h
        ih, iw = img.shape[:2]
        max_ratio = max(self._w_min / h, iw / float(ih))
        target_w = min(int(h * max_ratio), self._max_w)
        actual_w = min(int(math.ceil(h * iw / float(ih))), target_w)
        resized = cv2.resize(img, (actual_w, h))
        out = np.zeros((self._c, h, target_w), dtype=np.float32)
        if resized.dtype == np.uint8:
            out[:, :, :actual_w] = cv2.LUT(resized, self._lut).transpose(2, 0, 1)
        else:
            chw = np.transpose(resized.astype(np.float32, copy=False), (2, 0, 1))
            out[:, :, :actual_w] = (chw * (1.0 / 255.0) - 0.5) / 0.5
        return out


class _CTCDecode:
    """CTC greedy decode，字典第 0 位补 blank。"""

    def __init__(self, dict_path: Path):
        with open(dict_path, encoding="utf-8") as f:
            chars = tuple(line.rstrip("\n\r") for line in f)
        self._chars = ("blank", *chars)

    def __call__(self, output: np.ndarray):
        output = np.asarray(output)
        indices = output.argmax(axis=-1)
        probs = np.take_along_axis(output, indices[..., None], axis=-1)[..., 0]
        results = []
        for b in range(indices.shape[0]):
            seq = indices[b]
            keep = np.ones(seq.shape, dtype=bool)
            keep[1:] = seq[1:] != seq[:-1]
            keep &= seq != 0
            text = "".join(self._chars[i] for i in seq[keep])
            score = float(probs[b][keep].mean()) if keep.any() else 0.0
            results.append((text, score))
        return results


class V6Rec:
    """单条识别，接口和 OAS 的 ocr_single_line 对齐。"""

    def __init__(self, model_path: Path = MODEL_PATH, dict_path: Path = DICT_PATH):
        model_path = _ensure_model(model_path)
        self._session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        self._pre = _RecPreProcess()
        self._decode = _CTCDecode(dict_path)

    def recognize_single(self, image: np.ndarray):
        x = self._pre.single(image)[None, ...]
        out = self._session.run(None, {self._input_name: x})[0]
        return self._decode(out)[0]
