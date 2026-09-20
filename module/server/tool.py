# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import json
import multiprocessing
import re
import shutil
import threading
import time
import uuid
import base64
from dataclasses import dataclass
from pathlib import Path
from queue import Empty as QueueEmpty
from typing import Any

import cv2
import numpy as np
from filelock import FileLock

from dev_tools.assets_extract import AssetsExtractor
from dev_tools.assets_test import detect_image_detail, detect_ocr_detail
from module.config.atomicwrites import atomic_write
from module.config.config import Config
from module.logger import logger
from module.server.config_manager import ConfigManager
from module.server.annotator_rule_schema import (
    default_list_meta,
    field_default,
    field_options,
    get_rule_types,
    get_schema_payload,
    merge_list_meta_with_defaults,
    merge_rule_with_defaults,
)
from module.server.tool_capture_worker import run_annotator_capture_worker

PROJECT_ROOT = Path.cwd().resolve()
TASKS_ROOT = (PROJECT_ROOT / "tasks").resolve()
ANNOTATOR_ROOT = (PROJECT_ROOT / "log" / "annotator").resolve()

ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
ALLOWED_RULE_TYPE = set(get_rule_types())
ALLOWED_IMAGE_METHOD = set(field_options("image", "method"))
ALLOWED_OCR_MODE = set(field_options("ocr", "mode"))
ALLOWED_LIST_DIRECTION = set(field_options("list", "direction"))
ALLOWED_LIST_MODE = set(field_options("list", "type"))
ALLOWED_SWIPE_MODE = set(field_options("swipe", "mode"))

SESSION_IDLE_TIMEOUT_SECONDS = 10 * 60
SESSION_SWEEP_INTERVAL_SECONDS = 30
EMULATOR_CAPTURE_MAX_RETRIES = 3
# 本地截图方式（nemu_ipc / window_background 等）子进程能立刻响应 stop
EMULATOR_CAPTURE_PROCESS_JOIN_TIMEOUT_SECONDS = 2.5
# 网络截图方式（ADB / DroidCast / scrcpy 连远端设备）单次 screencap 就可能 1~3s，
# 子进程恰好卡在截图里时 2.5s 必然超时，需要放宽。
EMULATOR_CAPTURE_PROCESS_JOIN_TIMEOUT_NETWORK_SECONDS = 8.0
# terminate() 之后等它真正消失的时间（POSIX 上是 SIGTERM，可能被拖延）
EMULATOR_CAPTURE_PROCESS_TERMINATE_WAIT_SECONDS = 1.0
# kill() 之后等它真正消失的时间（POSIX 上是 SIGKILL，不可捕获）
EMULATOR_CAPTURE_PROCESS_KILL_WAIT_SECONDS = 1.0
EMULATOR_CAPTURE_COMMAND_TIMEOUT_SECONDS = 5.0

# 这些截图方式要经网络往返，视作“远端设备”
_REMOTE_SCREENSHOT_METHODS = {
    'ADB', 'ADB_nc', 'uiautomator2', 'DroidCast', 'DroidCast_raw', 'scrcpy',
}

_EMULATOR_CAPTURE_CONTEXT = multiprocessing.get_context("spawn")


class AnnotatorError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class SessionImage:
    image_id: str
    path: Path
    source: str
    original_name: str
    created_at: float

    def to_dict(self, session_id: str) -> dict[str, Any]:
        url = f"/tool/annotator/api/images/{session_id}/{self.image_id}"
        return {
            "id": self.image_id,
            "name": self.original_name,
            "source": self.source,
            "created_at": self.created_at,
            "url": url,
            "thumb_url": url,
        }


class EmulatorCaptureSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.state = "stopped"
        self.config_name = ""
        self.frame_rate = 2
        self.error = ""
        # 记录实际生效的截图方式，用于判断子进程停止时该等多久
        self.screenshot_method = ""
        self._process: multiprocessing.Process | None = None
        self._state_queue = None
        self._frame_queue = None
        self._command_queue = None
        self._response_queue = None
        self._frame_lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._updated_at = 0.0
        self._retry_count = 0
        self._max_retries = EMULATOR_CAPTURE_MAX_RETRIES
        self._last_error_at = 0.0

    def _drain_status_queue(self) -> None:
        # 把队列绑到局部变量：stop() 可能在别的线程里把属性置为 None 或 close()，
        # 先判 None 再取值会有竞态窗口。
        queue = self._state_queue
        if queue is None:
            return
        while True:
            try:
                payload = queue.get_nowait()
            except (QueueEmpty, ValueError, OSError):
                break

            with self._frame_lock:
                if "state" in payload:
                    self.state = str(payload["state"])
                if "error" in payload:
                    self.error = str(payload["error"] or "")
                if "retry_count" in payload:
                    self._retry_count = int(payload["retry_count"])
                if "max_retries" in payload:
                    self._max_retries = int(payload["max_retries"])
                if "last_error_at" in payload:
                    self._last_error_at = float(payload["last_error_at"] or 0.0)

    def _drain_frame_queue(self) -> None:
        queue = self._frame_queue
        if queue is None:
            return
        while True:
            try:
                payload = queue.get_nowait()
            except (QueueEmpty, ValueError, OSError):
                break

            jpeg = payload.get("jpeg")
            if not jpeg:
                continue

            with self._frame_lock:
                self._latest_jpeg = bytes(jpeg)
                self._updated_at = float(payload.get("updated_at") or time.time())
                self._retry_count = 0
                self.error = ""
                self.state = "running"

    def _sync_process_state(self) -> None:
        process = self._process
        if process is None or process.is_alive():
            return

        with self._frame_lock:
            if self.state in {"stopped", "error"}:
                return
            if process.exitcode in (None, 0):
                self.state = "stopped"
                return
            self.state = "error"
            self._last_error_at = time.time()
            if not self.error:
                self.error = f"模拟器采集进程异常退出({process.exitcode})"

    def _drain_updates(self) -> None:
        self._drain_status_queue()
        self._drain_frame_queue()
        self._sync_process_state()

    def _create_ipc(self) -> None:
        self._state_queue = _EMULATOR_CAPTURE_CONTEXT.Queue()
        self._frame_queue = _EMULATOR_CAPTURE_CONTEXT.Queue(maxsize=1)
        self._command_queue = _EMULATOR_CAPTURE_CONTEXT.Queue()
        self._response_queue = _EMULATOR_CAPTURE_CONTEXT.Queue()

    def _dispose_ipc(self) -> None:
        for name in ("_state_queue", "_frame_queue", "_command_queue", "_response_queue"):
            queue = getattr(self, name)
            if queue is None:
                continue
            try:
                queue.close()
            except Exception:
                pass
            setattr(self, name, None)

    def status(self) -> dict[str, Any]:
        self._drain_updates()
        with self._frame_lock:
            return {
                "state": self.state,
                "config_name": self.config_name,
                "frame_rate": self.frame_rate,
                "error": self.error,
                "updated_at": self._updated_at,
                "last_frame_at": self._updated_at,
                "retry_count": self._retry_count,
                "max_retries": self._max_retries,
                "last_error_at": self._last_error_at,
            }

    def start(self, config_name: str, frame_rate: int) -> int:
        self.stop(clear_error=True)
        with self._frame_lock:
            self.config_name = config_name
            self.frame_rate = max(1, min(int(frame_rate), 10))
            self.state = "starting"
            self.error = ""
            self._updated_at = 0.0
            self._retry_count = 0
            self._last_error_at = 0.0
            self._latest_jpeg = None
        self.screenshot_method = self._read_configured_screenshot_method()
        self._create_ipc()
        self._process = _EMULATOR_CAPTURE_CONTEXT.Process(
            target=run_annotator_capture_worker,
            args=(
                self.session_id,
                self.config_name,
                self.frame_rate,
                self._state_queue,
                self._frame_queue,
                self._command_queue,
                self._response_queue,
            ),
            name=f"annotator_capture_{self.session_id}",
            daemon=True,
        )
        self._process.start()
        return self.frame_rate

    def _read_configured_screenshot_method(self) -> str:
        """读取配置里的截图方式，用于决定停止子进程时该等多久。

        读不到就当作本地方式，保持原有的 2.5s 行为。
        """
        if not self.config_name:
            return ""
        try:
            config = Config(config_name=self.config_name)
            method = config.script.device.screenshot_method
        except Exception as e:
            logger.debug(f"[annotator] read screenshot method failed: {type(e).__name__}: {e}")
            return ""
        # 枚举值是 str 子类，转成纯字符串再比较
        return str(getattr(method, "value", method) or "")

    def _resolve_stop_timeout(self) -> float:
        """按截图方式决定等子进程退出的时长。"""
        if str(self.screenshot_method or "") in _REMOTE_SCREENSHOT_METHODS:
            return EMULATOR_CAPTURE_PROCESS_JOIN_TIMEOUT_NETWORK_SECONDS
        return EMULATOR_CAPTURE_PROCESS_JOIN_TIMEOUT_SECONDS

    def stop(self, clear_error: bool = False) -> None:
        """停止采集子进程。

        注意：本方法是同步阻塞的（最多几秒），调用方必须放在线程池里执行，
        不要直接在 async 路由里调用，否则会卡住事件循环。
        """
        process = self._process
        if process and process.is_alive() and self._command_queue is not None:
            try:
                self._command_queue.put_nowait({"type": "stop"})
            except Exception:
                pass

        forced_terminate = False
        still_alive = False
        if process and process.is_alive():
            join_timeout = self._resolve_stop_timeout()
            process.join(timeout=join_timeout)
            if process.is_alive():
                # 采集进程没响应 stop 命令，只能强杀。
                # Windows 的 terminate() 是 TerminateProcess（无条件立即杀），
                # 而 POSIX 只是 SIGTERM（可拖延），所以这里必须逐级升级并在最后复查存活，
                # 否则会出现“以为停了、其实子进程还在占着设备”的假停止。
                logger.warning(
                    f"[annotator] capture process did not exit within {join_timeout}s, "
                    f"terminating, session={self.session_id}"
                )
                process.terminate()
                process.join(timeout=EMULATOR_CAPTURE_PROCESS_TERMINATE_WAIT_SECONDS)
                forced_terminate = True

                if process.is_alive():
                    # SIGTERM 无效（常见于卡在 C 层阻塞调用里），升级为 SIGKILL。
                    # SIGKILL 不可捕获，语义上等价于 Windows 的 TerminateProcess。
                    logger.warning(
                        f"[annotator] capture process survived terminate, killing, "
                        f"session={self.session_id}, pid={process.pid}"
                    )
                    try:
                        process.kill()
                    except Exception as e:
                        logger.error(f"[annotator] kill capture process failed: {type(e).__name__}: {e}")
                    process.join(timeout=EMULATOR_CAPTURE_PROCESS_KILL_WAIT_SECONDS)
                    still_alive = process.is_alive()

        if still_alive:
            logger.error(
                f"[annotator] capture process still alive after kill, "
                f"session={self.session_id}, pid={process.pid}"
            )

        self._drain_updates()
        self._process = None
        self._dispose_ipc()
        with self._frame_lock:
            self.state = "stopped"
            self._retry_count = 0
            self._latest_jpeg = None
            self._updated_at = 0.0
            if still_alive:
                self.error = "采集进程无法被终止，仍在后台运行，请重启服务"
                self._last_error_at = time.time()
            elif forced_terminate:
                self.error = "采集进程未响应停止命令，已强制终止；设备侧连接可能未完全释放"
                self._last_error_at = time.time()
            elif clear_error:
                self.error = ""
                self._last_error_at = 0.0

    def latest_jpeg(self) -> bytes | None:
        self._drain_updates()
        with self._frame_lock:
            if self.state != "running" or self._latest_jpeg is None:
                return None
            return bytes(self._latest_jpeg)

    def capture_latest_frame(self, output_file: Path) -> None:
        self._drain_updates()
        with self._frame_lock:
            state = self.state
            error = self.error

        if state != "running":
            if state == "error":
                raise AnnotatorError("emulator_error", f"模拟器画面不可用: {error or 'unknown'}", 409)
            if state == "starting":
                raise AnnotatorError("emulator_starting", "模拟器画面尚未就绪，请稍后重试", 409)
            raise AnnotatorError("emulator_not_running", "模拟器画面未启动", 409)

        if self._command_queue is None or self._response_queue is None:
            raise AnnotatorError("emulator_not_running", "模拟器画面未启动", 409)

        request_id = uuid.uuid4().hex
        try:
            self._command_queue.put_nowait(
                {
                    "type": "capture",
                    "request_id": request_id,
                    "output_file": str(output_file.resolve()),
                }
            )
        except Exception as e:
            raise AnnotatorError("capture_failed", "提交截图请求失败", 500) from e

        deadline = time.time() + EMULATOR_CAPTURE_COMMAND_TIMEOUT_SECONDS
        while time.time() < deadline:
            self._drain_updates()
            wait_seconds = min(0.1, deadline - time.time())
            if wait_seconds <= 0:
                break
            try:
                response = self._response_queue.get(timeout=wait_seconds)
            except QueueEmpty:
                continue

            if str(response.get("request_id", "")) != request_id:
                continue
            if response.get("ok"):
                return

            code = str(response.get("code", "")).strip() or "capture_failed"
            message = str(response.get("message", "")).strip() or "保存截图失败"
            status_code = 500 if code == "capture_failed" else 400
            raise AnnotatorError(code, message, status_code)

        self._drain_updates()
        raise AnnotatorError("capture_timeout", "保存截图超时", 504)


class AnnotatorSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.root = ANNOTATOR_ROOT / session_id
        self.upload_dir = self.root / "uploads"
        self.capture_dir = self.root / "captures"
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.images: dict[str, SessionImage] = {}
        self.image_order: list[str] = []
        self.current_image_id: str | None = None
        self.lock = threading.Lock()
        self.capture_session = EmulatorCaptureSession(session_id)
        self.last_active_at = time.time()

    def touch(self) -> float:
        now = time.time()
        with self.lock:
            self.last_active_at = now
        return now

    def get_last_active_at(self) -> float:
        with self.lock:
            return self.last_active_at

    def shutdown(self) -> None:
        self.capture_session.stop(clear_error=True)

    def add_image(self, image_file: Path, source: str, original_name: str) -> SessionImage:
        image_id = uuid.uuid4().hex
        image = SessionImage(
            image_id=image_id,
            path=image_file,
            source=source,
            original_name=original_name,
            created_at=time.time(),
        )
        with self.lock:
            self.images[image_id] = image
            self.image_order.append(image_id)
            self.current_image_id = image_id
            self.last_active_at = time.time()
        return image

    def remove_image(self, image_id: str) -> bool:
        with self.lock:
            image = self.images.pop(image_id, None)
            if image is None:
                return False
            self.image_order = [x for x in self.image_order if x != image_id]
            if self.current_image_id == image_id:
                self.current_image_id = self.image_order[-1] if self.image_order else None
            self.last_active_at = time.time()
        try:
            if image.path.exists():
                image.path.unlink()
        except Exception:
            logger.warning(f"[annotator] remove image file failed: {image.path}")
        return True

    def remove_images(self, image_ids: list[str]) -> int:
        removed = 0
        for image_id in image_ids:
            if self.remove_image(image_id):
                removed += 1
        return removed

    def clear_images(self) -> int:
        with self.lock:
            image_ids = list(self.image_order)
        return self.remove_images(image_ids)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            images = [self.images[i].to_dict(self.session_id) for i in self.image_order if i in self.images]
            return {
                "session_id": self.session_id,
                "current_image_id": self.current_image_id,
                "updated_at": self.last_active_at,
                "images": images,
                "emulator": self.capture_session.status(),
            }


class AnnotatorManager:
    def __init__(self) -> None:
        self._sessions: dict[str, AnnotatorSession] = {}
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._last_cleanup_at = 0.0
        self._session_idle_timeout = SESSION_IDLE_TIMEOUT_SECONDS
        self._session_sweep_interval = SESSION_SWEEP_INTERVAL_SECONDS
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._session_cleanup_loop,
            daemon=True,
            name="annotator_session_cleanup",
        )
        ANNOTATOR_ROOT.mkdir(parents=True, exist_ok=True)
        self._cleanup_thread.start()

    def _session_cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(self._session_sweep_interval):
            try:
                self._cleanup_expired_sessions(force=True)
            except Exception:
                logger.exception("[annotator] cleanup loop failed")

    def _cleanup_expired_sessions(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_cleanup_at < self._session_sweep_interval:
            return

        with self._cleanup_lock:
            now = time.time()
            if not force and now - self._last_cleanup_at < self._session_sweep_interval:
                return
            self._last_cleanup_at = now

            expired: list[str] = []
            with self._lock:
                for session_id, session in self._sessions.items():
                    idle_seconds = now - session.get_last_active_at()
                    if idle_seconds >= self._session_idle_timeout:
                        expired.append(session_id)

        for session_id in expired:
            self.close_session(session_id, reason="timeout")

    @staticmethod
    def index_file() -> Path:
        return Path(__file__).resolve().parent / "web" / "annotator" / "index.html"

    @staticmethod
    def static_dir() -> Path:
        return Path(__file__).resolve().parent / "web" / "annotator"

    def _cleanup_session_dir_path(self, session_id: str, target: Path, reason: str) -> bool:
        target = target.resolve()
        self._ensure_within_root(target, ANNOTATOR_ROOT)
        if target == ANNOTATOR_ROOT:
            raise AnnotatorError("invalid_path", "会话目录无效", 500)

        if not target.exists():
            return False

        if not target.is_dir():
            logger.warning(
                f"[annotator] skip non-dir session cleanup, session={session_id}, reason={reason}, target={target}"
            )
            return False

        shutil.rmtree(target)
        logger.info(f"[annotator] session dir removed, session={session_id}, reason={reason}, dir={target}")
        return True

    def _cleanup_session_dir(self, session: AnnotatorSession, reason: str) -> bool:
        return self._cleanup_session_dir_path(session.session_id, session.root, reason)

    def _cleanup_foreign_session_dirs(self, keep_session_id: str, reason: str) -> int:
        removed = 0
        if not ANNOTATOR_ROOT.exists():
            return removed

        for target in ANNOTATOR_ROOT.iterdir():
            if target.name == keep_session_id:
                continue
            try:
                if self._cleanup_session_dir_path(target.name, target, reason):
                    removed += 1
            except Exception:
                logger.exception(
                    f"[annotator] foreign session dir cleanup failed, session={target.name}, reason={reason}"
                )
        return removed

    def _replace_active_sessions(self, session: AnnotatorSession, reason: str) -> int:
        with self._lock:
            replaced_sessions = [
                (session_id, existing)
                for session_id, existing in self._sessions.items()
                if session_id != session.session_id
            ]
            self._sessions = {session.session_id: session}

        for session_id, existing in replaced_sessions:
            try:
                existing.shutdown()
            except Exception:
                logger.exception(
                    f"[annotator] old session shutdown failed, session={session_id}, reason={reason}"
                )
            try:
                self._cleanup_session_dir(existing, reason)
            except Exception:
                logger.exception(
                    f"[annotator] old session dir cleanup failed, session={session_id}, reason={reason}"
                )
        return len(replaced_sessions)

    def close_session(self, session_id: str, reason: str = "manual", raise_if_missing: bool = False) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            if raise_if_missing:
                raise AnnotatorError("invalid_session", f"会话不存在: {session_id}", 404)
            return {
                "session_id": session_id,
                "closed": False,
                "reason": reason,
                "dir_removed": False,
            }

        session.shutdown()

        dir_removed = False
        try:
            dir_removed = self._cleanup_session_dir(session, reason)
        except Exception:
            logger.exception(f"[annotator] session dir cleanup failed, session={session_id}, reason={reason}")

        logger.info(
            f"[annotator] session closed, session={session_id}, reason={reason}, dir_removed={dir_removed}"
        )
        return {
            "session_id": session_id,
            "closed": True,
            "reason": reason,
            "dir_removed": dir_removed,
        }

    def create_session(self) -> dict[str, Any]:
        self._cleanup_expired_sessions()
        with self._cleanup_lock:
            session_id = uuid.uuid4().hex
            session = AnnotatorSession(session_id)
            replace_reason = f"replaced_by:{session_id}"
            replaced_count = self._replace_active_sessions(session, replace_reason)
            orphan_removed = self._cleanup_foreign_session_dirs(
                keep_session_id=session_id,
                reason=f"create_session:{session_id}",
            )
        logger.info(
            f"[annotator] session created, session={session_id}, "
            f"replaced_sessions={replaced_count}, removed_other_dirs={orphan_removed}"
        )
        return session.snapshot()

    def _get_session(self, session_id: str) -> AnnotatorSession:
        self._cleanup_expired_sessions()
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise AnnotatorError("invalid_session", f"会话不存在: {session_id}", 404)
        session.touch()
        return session

    def get_session_snapshot(self, session_id: str) -> dict[str, Any]:
        return self._get_session(session_id).snapshot()

    @staticmethod
    def _safe_stem(filename: str) -> str:
        stem = Path(filename).stem.strip()
        stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem)
        return stem or "image"

    def save_uploaded_images_base64(self, session_id: str, images_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        session = self._get_session(session_id)
        results: list[dict[str, Any]] = []
        for payload in images_payload:
            filename = str(payload.get("name", "")).strip()
            content_base64 = str(payload.get("content_base64", "")).strip()
            if not filename:
                continue
            if not content_base64:
                continue
            suffix = Path(filename).suffix.lower()
            if not suffix:
                suffix = ".png"
            if suffix not in ALLOWED_IMAGE_EXT:
                raise AnnotatorError("invalid_image_ext", f"不支持的图片类型: {suffix}", 400)
            image_name = f"{uuid.uuid4().hex}_{self._safe_stem(filename)}{suffix}"
            target = session.upload_dir / image_name
            if "," in content_base64:
                content_base64 = content_base64.split(",", 1)[1]
            try:
                content = base64.b64decode(content_base64)
            except Exception as e:
                raise AnnotatorError("invalid_image_data", f"图片编码解析失败: {filename}", 400) from e
            target.write_bytes(content)
            image = session.add_image(target, "upload", filename)
            results.append(image.to_dict(session_id))
        if not results:
            raise AnnotatorError("empty_upload", "未上传有效图片", 400)
        logger.info(f"[annotator] upload images, session={session_id}, count={len(results)}")
        return results

    def list_images(self, session_id: str) -> list[dict[str, Any]]:
        session = self._get_session(session_id)
        return session.snapshot()["images"]

    def get_image_file(self, session_id: str, image_id: str) -> Path:
        session = self._get_session(session_id)
        image = session.images.get(image_id)
        if image is None:
            raise AnnotatorError("image_not_found", f"图片不存在: {image_id}", 404)
        return image.path

    def delete_image(self, session_id: str, image_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        removed = session.remove_image(image_id)
        if not removed:
            raise AnnotatorError("image_not_found", f"图片不存在: {image_id}", 404)
        logger.info(f"[annotator] image removed, session={session_id}, image={image_id}")
        return session.snapshot()

    def delete_images(self, session_id: str, image_ids: list[str]) -> dict[str, Any]:
        if not image_ids:
            raise AnnotatorError("empty_image_ids", "image_ids 不能为空", 400)
        session = self._get_session(session_id)
        removed = session.remove_images(image_ids)
        logger.info(f"[annotator] images removed, session={session_id}, removed={removed}")
        return {"removed_count": removed, "session": session.snapshot()}

    def clear_images(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        removed = session.clear_images()
        logger.info(f"[annotator] images cleared, session={session_id}, removed={removed}")
        return {"removed_count": removed, "session": session.snapshot()}

    @staticmethod
    def list_task_names() -> list[str]:
        tasks = []
        for d in TASKS_ROOT.iterdir():
            if not d.is_dir():
                continue
            if d.name.startswith("__"):
                continue
            tasks.append(d.name)
        return sorted(tasks)

    def list_configs(self) -> list[str]:
        return ConfigManager.all_script_files()

    @staticmethod
    def rule_schema() -> dict[str, Any]:
        return get_schema_payload()

    def start_emulator(self, session_id: str, config_name: str, frame_rate: int) -> dict[str, Any]:
        session = self._get_session(session_id)
        if config_name not in self.list_configs():
            raise AnnotatorError("invalid_config", f"配置不存在: {config_name}", 400)
        applied_rate = session.capture_session.start(config_name, frame_rate)
        logger.info(
            f"[annotator] emulator start, session={session_id}, config={config_name}, frame_rate={applied_rate}"
        )
        return session.capture_session.status()

    def stop_emulator(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        session.capture_session.stop(clear_error=True)
        logger.info(f"[annotator] emulator stop, session={session_id}")
        return session.capture_session.status()

    def emulator_status(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        return session.capture_session.status()

    def latest_emulator_frame(self, session_id: str) -> bytes | None:
        session = self._get_session(session_id)
        return session.capture_session.latest_jpeg()

    def capture_from_emulator(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        target = session.capture_dir / f"capture_{int(time.time() * 1000)}.png"
        session.capture_session.capture_latest_frame(target)
        image = session.add_image(target, "capture", target.name)
        logger.info(f"[annotator] capture frame, session={session_id}, target={target}")
        return image.to_dict(session_id)

    @staticmethod
    def _format_roi_number(value: float) -> str:
        rounded = round(value)
        if abs(value - rounded) < 1e-6:
            return str(int(rounded))
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _parse_roi(value: str) -> str:
        if not isinstance(value, str):
            raise AnnotatorError("invalid_roi", "ROI 必须是字符串", 400)
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 4:
            raise AnnotatorError("invalid_roi", "ROI 必须是 x,y,w,h", 400)
        try:
            x, y, w, h = [float(p) for p in parts]
        except ValueError as e:
            raise AnnotatorError("invalid_roi", "ROI 必须是数字", 400) from e
        if w <= 0 or h <= 0:
            raise AnnotatorError("invalid_roi", "ROI 的宽高必须大于 0", 400)
        return ",".join(
            [
                AnnotatorManager._format_roi_number(x),
                AnnotatorManager._format_roi_number(y),
                AnnotatorManager._format_roi_number(w),
                AnnotatorManager._format_roi_number(h),
            ]
        )

    def _normalize_image_rules(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, rule in enumerate(rules):
            item_name = str(rule.get("itemName", "")).strip()
            if not item_name:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条规则缺少 itemName", 400)
            image_name = str(rule.get("imageName", "")).strip() or f"{item_name}.png"
            method = str(rule.get("method", field_default("image", "method", "Template matching"))).strip() or field_default("image", "method", "Template matching")
            if method not in ALLOWED_IMAGE_METHOD:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条规则 method 不支持: {method}", 400)
            threshold_value = rule.get("threshold", 0.8)
            try:
                threshold = float(threshold_value)
            except (TypeError, ValueError) as e:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条规则 threshold 非法", 400) from e
            if threshold < 0 or threshold > 1:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条规则 threshold 必须在 0-1", 400)
            normalized.append(
                {
                    "itemName": item_name,
                    "imageName": image_name,
                    "roiFront": self._parse_roi(str(rule.get("roiFront", ""))),
                    "roiBack": self._parse_roi(str(rule.get("roiBack", ""))),
                    "method": method,
                    "threshold": threshold,
                    "description": str(rule.get("description", "")).strip(),
                }
            )
        return normalized

    def _normalize_ocr_rules(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, rule in enumerate(rules):
            item_name = str(rule.get("itemName", "")).strip()
            if not item_name:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条规则缺少 itemName", 400)
            mode = str(rule.get("mode", field_default("ocr", "mode", "Single"))).strip() or field_default("ocr", "mode", "Single")
            if mode not in ALLOWED_OCR_MODE:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条规则 mode 不支持: {mode}", 400)
            normalized.append(
                {
                    "itemName": item_name,
                    "roiFront": self._parse_roi(str(rule.get("roiFront", ""))),
                    "roiBack": self._parse_roi(str(rule.get("roiBack", ""))),
                    "mode": mode,
                    "method": str(rule.get("method", field_default("ocr", "method", "Default"))).strip() or field_default("ocr", "method", "Default"),
                    "keyword": str(rule.get("keyword", "")).strip(),
                    "description": str(rule.get("description", "")).strip(),
                }
            )
        return normalized

    def _normalize_click_rules(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, rule in enumerate(rules):
            item_name = str(rule.get("itemName", "")).strip()
            if not item_name:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条点击规则缺少 itemName", 400)
            normalized.append(
                {
                    "itemName": item_name,
                    "roiFront": self._parse_roi(str(rule.get("roiFront", ""))),
                    "roiBack": self._parse_roi(str(rule.get("roiBack", ""))),
                    "description": str(rule.get("description", "")).strip(),
                }
            )
        return normalized

    def _normalize_swipe_rules(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, rule in enumerate(rules):
            item_name = str(rule.get("itemName", "")).strip()
            if not item_name:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条滑动规则缺少 itemName", 400)
            mode = str(rule.get("mode", field_default("swipe", "mode", "default"))).strip() or field_default("swipe", "mode", "default")
            if mode not in ALLOWED_SWIPE_MODE:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条滑动规则 mode 不支持: {mode}", 400)
            normalized.append(
                {
                    "itemName": item_name,
                    "roiFront": self._parse_roi(str(rule.get("roiFront", ""))),
                    "roiBack": self._parse_roi(str(rule.get("roiBack", ""))),
                    "mode": mode,
                    "description": str(rule.get("description", "")).strip(),
                }
            )
        return normalized

    def _normalize_long_click_rules(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, rule in enumerate(rules):
            item_name = str(rule.get("itemName", "")).strip()
            if not item_name:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条长按规则缺少 itemName", 400)
            try:
                duration = int(rule.get("duration", field_default("long_click", "duration", 1000)))
            except (TypeError, ValueError) as e:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条长按规则 duration 非法", 400) from e
            if duration <= 0:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条长按规则 duration 必须大于 0", 400)
            normalized.append(
                {
                    "itemName": item_name,
                    "roiFront": self._parse_roi(str(rule.get("roiFront", ""))),
                    "roiBack": self._parse_roi(str(rule.get("roiBack", ""))),
                    "duration": duration,
                    "description": str(rule.get("description", "")).strip(),
                }
            )
        return normalized

    def _normalize_list_rules(self, rules: list[dict[str, Any]], list_meta: dict[str, Any] | None) -> dict[str, Any]:
        if not list_meta:
            raise AnnotatorError("invalid_rule", "list 规则缺少 list_meta", 400)

        list_name = str(list_meta.get("name", "")).strip()
        if not list_name:
            raise AnnotatorError("invalid_rule", "list_meta.name 不能为空", 400)

        direction = str(list_meta.get("direction", field_default("list", "direction", "vertical"))).strip() or field_default("list", "direction", "vertical")
        if direction not in ALLOWED_LIST_DIRECTION:
            raise AnnotatorError("invalid_rule", f"list_meta.direction 不支持: {direction}", 400)

        list_type = str(list_meta.get("type", field_default("list", "type", "image"))).strip() or field_default("list", "type", "image")
        if list_type not in ALLOWED_LIST_MODE:
            raise AnnotatorError("invalid_rule", f"list_meta.type 不支持: {list_type}", 400)

        roi_back = self._parse_roi(str(list_meta.get("roiBack", "")))

        normalized_items = []
        for index, item in enumerate(rules):
            item_name = str(item.get("itemName", "")).strip()
            if not item_name:
                raise AnnotatorError("invalid_rule", f"第 {index + 1} 条 list 项缺少 itemName", 400)
            normalized_items.append(
                {
                    "itemName": item_name,
                    "roiFront": self._parse_roi(str(item.get("roiFront", ""))),
                }
            )

        return {
            "name": list_name,
            "direction": direction,
            "type": list_type,
            "roiBack": roi_back,
            "description": str(list_meta.get("description", "")).strip(),
            "list": normalized_items,
        }

    @staticmethod
    def _ensure_within_root(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as e:
            raise AnnotatorError("invalid_path", "目标路径不在允许目录内", 400) from e

    @staticmethod
    def _resolve_task_root(task_name: str) -> Path:
        task_root = (TASKS_ROOT / task_name).resolve()
        AnnotatorManager._ensure_within_root(task_root, TASKS_ROOT)
        if not task_root.exists() or not task_root.is_dir():
            raise AnnotatorError("invalid_task", f"任务目录不存在: {task_name}", 404)
        return task_root

    @staticmethod
    def _resolve_tasks_subdir(rel_dir: str) -> Path:
        rel = Path(str(rel_dir or "").strip().replace("\\", "/"))
        if rel.is_absolute():
            raise AnnotatorError("invalid_path", "目录路径必须是相对路径", 400)
        base = (TASKS_ROOT / rel).resolve()
        AnnotatorManager._ensure_within_root(base, TASKS_ROOT)
        if not base.exists() or not base.is_dir():
            raise AnnotatorError("invalid_path", f"目录不存在: {rel.as_posix()}", 404)
        return base

    @staticmethod
    def _resolve_json_path(task_name: str, json_relpath: str) -> tuple[Path, Path]:
        task_root = AnnotatorManager._resolve_task_root(task_name)
        rel = Path(json_relpath)
        if rel.is_absolute():
            raise AnnotatorError("invalid_path", "json_relpath 必须是相对路径", 400)
        target_json = (task_root / rel).resolve()
        AnnotatorManager._ensure_within_root(target_json, task_root)
        if target_json.suffix.lower() != ".json":
            raise AnnotatorError("invalid_path", "目标文件必须是 .json", 400)
        return task_root, target_json

    @staticmethod
    def _resolve_assets_extract_target(task_root: Path, target_json: Path) -> tuple[Path, Path]:
        task_root = task_root.resolve()
        target_json = target_json.resolve()
        AnnotatorManager._ensure_within_root(target_json, task_root)

        if task_root.name != "Component":
            return task_root, task_root / "assets.py"

        rel_parts = target_json.relative_to(task_root).parts
        if len(rel_parts) < 2:
            raise AnnotatorError(
                "invalid_component_task",
                "Component 目录下的规则文件必须位于具体组件子任务目录中",
                400,
            )

        component_name = rel_parts[0]
        extract_root = (task_root / component_name).resolve()
        AnnotatorManager._ensure_within_root(extract_root, task_root)
        if not extract_root.exists() or not extract_root.is_dir():
            raise AnnotatorError(
                "invalid_component_task",
                f"Component 子任务目录不存在: {component_name}",
                404,
            )
        return extract_root, extract_root / "assets.py"

    @staticmethod
    def list_task_json_files(task_name: str) -> list[str]:
        task_root = AnnotatorManager._resolve_task_root(task_name)
        result = []
        for p in task_root.rglob("*.json"):
            if p.is_file():
                result.append(str(p.relative_to(task_root).as_posix()))
        return sorted(result)

    @staticmethod
    def list_rule_source(rel_dir: str = "") -> dict[str, Any]:
        base = AnnotatorManager._resolve_tasks_subdir(rel_dir)
        directories: list[str] = []
        json_files: list[str] = []
        for item in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if item.is_dir():
                if item.name.startswith("__"):
                    continue
                directories.append(item.name)
            elif item.is_file() and item.suffix.lower() == ".json":
                json_files.append(item.name)

        base_rel = ""
        if base != TASKS_ROOT:
            base_rel = str(base.relative_to(TASKS_ROOT).as_posix())

        return {
            "base_dir": base_rel,
            "directories": directories,
            "json_files": json_files,
        }

    @staticmethod
    def create_rule_json(rel_dir: str, file_name: str) -> dict[str, Any]:
        base = AnnotatorManager._resolve_tasks_subdir(rel_dir)
        name = str(file_name or "").strip()
        if not name:
            raise AnnotatorError("invalid_name", "文件名不能为空", 400)
        if "/" in name or "\\" in name:
            raise AnnotatorError("invalid_name", "文件名不能包含路径分隔符", 400)
        if not name.lower().endswith(".json"):
            name = f"{name}.json"
        target = (base / name).resolve()
        AnnotatorManager._ensure_within_root(target, TASKS_ROOT)
        if target.exists():
            raise AnnotatorError("file_exists", f"文件已存在: {name}", 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(target, overwrite=True, encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        return {"file": str(target.relative_to(TASKS_ROOT).as_posix())}

    @staticmethod
    def delete_rule_json(rel_dir: str, file_name: str) -> dict[str, Any]:
        base = AnnotatorManager._resolve_tasks_subdir(rel_dir)
        name = str(file_name or "").strip()
        if not name:
            raise AnnotatorError("invalid_name", "文件名不能为空", 400)
        if "/" in name or "\\" in name:
            raise AnnotatorError("invalid_name", "文件名不能包含路径分隔符", 400)
        target = (base / name).resolve()
        AnnotatorManager._ensure_within_root(target, TASKS_ROOT)
        if target.suffix.lower() != ".json":
            raise AnnotatorError("invalid_name", "仅支持删除 .json 文件", 400)
        if not target.exists():
            raise AnnotatorError("json_not_found", f"JSON 文件不存在: {name}", 404)
        target.unlink()
        return {"file": str(target.relative_to(TASKS_ROOT).as_posix())}

    @staticmethod
    def _detect_rule_type(data: Any) -> str:
        if isinstance(data, dict) and "list" in data and "name" in data:
            return "list"
        if isinstance(data, list) and data:
            item = data[0]
            if not isinstance(item, dict):
                return "unknown"
            if "imageName" in item:
                return "image"
            if "keyword" in item:
                return "ocr"
            if "duration" in item:
                return "long_click"
            if "mode" in item:
                return "swipe"
            return "click"
        return "unknown"

    @staticmethod
    def load_rule_file(task_name: str, json_relpath: str) -> dict[str, Any]:
        task_root, target_json = AnnotatorManager._resolve_json_path(task_name, json_relpath)
        if not target_json.exists():
            raise AnnotatorError("json_not_found", f"JSON 文件不存在: {json_relpath}", 404)

        with open(target_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        rule_type = AnnotatorManager._detect_rule_type(data)

        list_meta = None
        rules: list[dict[str, Any]] = []

        if rule_type == "list":
            list_meta = {
                "name": str(data.get("name", field_default("list", "name", "list_name"))),
                "direction": str(data.get("direction", field_default("list", "direction", "vertical"))),
                "type": str(data.get("type", field_default("list", "type", "image"))),
                "roiBack": str(data.get("roiBack", default_list_meta().get("roiBack", "0,0,100,100"))),
                "description": str(data.get("description", field_default("list", "description", ""))),
            }
            for item in data.get("list", []):
                if not isinstance(item, dict):
                    continue
                rules.append(
                    {
                        "itemName": str(item.get("itemName", "")),
                        "roiFront": str(item.get("roiFront", "0,0,100,100")),
                    }
                )
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                if rule_type == "image":
                    rules.append(
                        {
                            "itemName": str(item.get("itemName", "")),
                            "imageName": str(item.get("imageName", "")),
                            "roiFront": str(item.get("roiFront", "0,0,100,100")),
                            "roiBack": str(item.get("roiBack", "0,0,100,100")),
                            "method": str(item.get("method", "Template matching")),
                            "threshold": float(item.get("threshold", 0.8)),
                            "description": str(item.get("description", "")),
                        }
                    )
                elif rule_type == "ocr":
                    rules.append(
                        {
                            "itemName": str(item.get("itemName", "")),
                            "roiFront": str(item.get("roiFront", "0,0,100,100")),
                            "roiBack": str(item.get("roiBack", "0,0,100,100")),
                            "mode": str(item.get("mode", "Single")),
                            "method": str(item.get("method", "Default")),
                            "keyword": str(item.get("keyword", "")),
                            "description": str(item.get("description", "")),
                        }
                    )
                elif rule_type == "click":
                    rules.append(
                        {
                            "itemName": str(item.get("itemName", "")),
                            "roiFront": str(item.get("roiFront", "0,0,100,100")),
                            "roiBack": str(item.get("roiBack", "0,0,100,100")),
                            "description": str(item.get("description", "")),
                        }
                    )
                elif rule_type == "swipe":
                    rules.append(
                        {
                            "itemName": str(item.get("itemName", "")),
                            "roiFront": str(item.get("roiFront", "0,0,100,100")),
                            "roiBack": str(item.get("roiBack", "0,0,100,100")),
                            "mode": str(item.get("mode", "default")),
                            "description": str(item.get("description", "")),
                        }
                    )
                elif rule_type == "long_click":
                    rules.append(
                        {
                            "itemName": str(item.get("itemName", "")),
                            "roiFront": str(item.get("roiFront", "0,0,100,100")),
                            "roiBack": str(item.get("roiBack", "0,0,100,100")),
                            "duration": int(item.get("duration", 1000)),
                            "description": str(item.get("description", "")),
                        }
                    )

        if rule_type == "list":
            list_meta = merge_list_meta_with_defaults(list_meta)
        rules = [merge_rule_with_defaults(rule_type, item) for item in rules]

        rule_type_locked = rule_type in ALLOWED_RULE_TYPE and len(rules) > 0

        return {
            "task_name": task_name,
            "json_relpath": str(target_json.relative_to(task_root).as_posix()),
            "rule_type": rule_type,
            "rule_type_locked": rule_type_locked,
            "rules": rules,
            "list_meta": list_meta,
        }

    @staticmethod
    def _resolve_rule_image_path(task_root: Path, target_json: Path, image_name: str) -> tuple[Path, str]:
        image_rel = str(image_name or "").strip()
        if not image_rel:
            raise AnnotatorError("invalid_path", "image_name 不能为空", 400)
        rel = Path(image_rel)
        if rel.is_absolute():
            raise AnnotatorError("invalid_path", "image_name 必须是相对路径", 400)
        target = (target_json.parent / rel).resolve()
        AnnotatorManager._ensure_within_root(target, task_root)
        if target.suffix.lower() not in ALLOWED_IMAGE_EXT:
            raise AnnotatorError("invalid_image_ext", "目标图片后缀不受支持", 400)
        return target, image_rel

    @staticmethod
    def get_rule_image_file(task_name: str, json_relpath: str, image_name: str) -> Path:
        task_root, target_json = AnnotatorManager._resolve_json_path(task_name, json_relpath)
        target, image_rel = AnnotatorManager._resolve_rule_image_path(task_root, target_json, image_name)
        if not target.exists() or not target.is_file():
            raise AnnotatorError("image_not_found", f"图片不存在: {image_rel}", 404)
        return target

    def delete_rule_image(self, task_name: str, json_relpath: str, image_name: str) -> dict[str, Any]:
        task_root, target_json = self._resolve_json_path(task_name, json_relpath)
        target, image_rel = self._resolve_rule_image_path(task_root, target_json, image_name)

        if target.exists() and not target.is_file():
            raise AnnotatorError("invalid_path", f"目标图片不是文件: {image_rel}", 400)

        removed = False
        if target.exists():
            try:
                target.unlink()
            except OSError as e:
                raise AnnotatorError("delete_image_failed", f"删除图片失败: {image_rel}", 500) from e
            removed = True

        logger.info(
            f"[annotator] delete rule image, task={task_name}, target={target}, removed={removed}"
        )

        return {
            "removed": removed,
            "image_name": image_rel,
            "target_image": str(target.relative_to(PROJECT_ROOT).as_posix()),
            "target_json": str(target_json.relative_to(task_root).as_posix()),
        }

    @staticmethod
    def _list_item_image_name(item_name: str) -> str:
        name = str(item_name or "").strip()
        if not name:
            raise AnnotatorError("invalid_rule", "list 项缺少 itemName", 400)
        return f"{name}.png"

    @classmethod
    def get_list_item_image_file(cls, task_name: str, json_relpath: str, item_name: str) -> Path:
        return cls.get_rule_image_file(task_name, json_relpath, cls._list_item_image_name(item_name))

    @staticmethod
    def _parse_roi_tuple(value: str) -> tuple[int, int, int, int]:
        roi = AnnotatorManager._parse_roi(value)
        x, y, w, h = [float(v) for v in roi.split(",")]
        return int(round(x)), int(round(y)), int(round(w)), int(round(h))

    @staticmethod
    def _load_session_bgr_image(session: AnnotatorSession, image_id: str) -> tuple[np.ndarray, Path]:
        image = session.images.get(image_id)
        if image is None:
            raise AnnotatorError("image_not_found", f"图片不存在: {image_id}", 404)
        if not image.path.exists():
            raise AnnotatorError("image_not_found", "源图片文件不存在", 404)
        raw = cv2.imread(str(image.path), cv2.IMREAD_COLOR)
        if raw is None:
            raise AnnotatorError("invalid_image_data", "源图片读取失败", 400)
        return raw, image.path

    def test_rule(
        self,
        session_id: str,
        image_id: str,
        task_name: str,
        json_relpath: str,
        rule_type: str,
        rule: dict[str, Any],
        list_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        _, source_path = self._load_session_bgr_image(session, image_id)

        if rule_type == "image":
            item_name = str(rule.get("itemName", "")).strip() or "unnamed"
            image_name = str(rule.get("imageName", "")).strip()
            if not image_name:
                raise AnnotatorError("invalid_rule", "image 规则缺少 imageName", 400)
            method = str(rule.get("method", field_default("image", "method", "Template matching"))).strip() or field_default("image", "method", "Template matching")
            try:
                threshold = float(rule.get("threshold", 0.8))
            except (TypeError, ValueError) as e:
                raise AnnotatorError("invalid_rule", "threshold 非法", 400) from e
            roi_back = self._parse_roi_tuple(str(rule.get("roiBack", "")))
            task_root, target_json = self._resolve_json_path(task_name, json_relpath)
            template = self.get_rule_image_file(task_name, json_relpath, image_name)
            from module.atom.image import RuleImage

            target = RuleImage(
                roi_front=self._parse_roi_tuple(str(rule.get("roiFront", "0,0,100,100"))),
                roi_back=roi_back,
                method=method,
                threshold=threshold,
                file=str(template),
            )
            detail = detect_image_detail(str(source_path), target)
            return {
                "rule_type": "image",
                "item_name": item_name,
                "image_name": image_name,
                "matched": bool(detail.get("matched", False)),
                "similarity": float(detail.get("similarity", 0.0)),
                "roiFront": self._parse_roi(str(detail.get("roiFront", rule.get("roiFront", "0,0,100,100")))),
                "roiBack": self._parse_roi(str(detail.get("roiBack", rule.get("roiBack", "0,0,100,100")))),
                "threshold": threshold,
                "message": str(detail.get("message", "not_match")),
                "target_json": str(target_json.relative_to(task_root).as_posix()),
            }

        if rule_type == "ocr":
            item_name = str(rule.get("itemName", "")).strip() or "unnamed"
            mode = str(rule.get("mode", field_default("ocr", "mode", "Single"))).strip() or field_default("ocr", "mode", "Single")
            method = str(rule.get("method", "Default")).strip() or "Default"
            keyword = str(rule.get("keyword", "")).strip()
            roi_front = self._parse_roi_tuple(str(rule.get("roiFront", "")))
            roi_back = self._parse_roi_tuple(str(rule.get("roiBack", "")))
            from module.atom.ocr import RuleOcr

            target = RuleOcr(
                name=item_name,
                mode=mode,
                method=method,
                roi=roi_front,
                area=roi_back,
                keyword=keyword,
            )
            detail = detect_ocr_detail(str(source_path), target)
            return {
                "rule_type": "ocr",
                "item_name": item_name,
                "matched": bool(detail.get("matched", False)),
                "similarity": float(detail.get("similarity", 0.0)),
                "text": str(detail.get("text", "")),
                "keyword": keyword,
                "roiFront": self._parse_roi(str(detail.get("roiFront", rule.get("roiFront", "0,0,100,100")))),
                "roiBack": self._parse_roi(str(detail.get("roiBack", rule.get("roiBack", "0,0,100,100")))),
                "message": str(detail.get("message", "not_match")),
            }

        if rule_type == "list":
            if not list_meta:
                raise AnnotatorError("invalid_rule", "list 测试缺少 list_meta", 400)

            item_name = str(rule.get("itemName", "")).strip()
            if not item_name:
                raise AnnotatorError("invalid_rule", "list 项缺少 itemName", 400)

            list_type = str(list_meta.get("type", field_default("list", "type", "image"))).strip() or field_default("list", "type", "image")
            if list_type not in ALLOWED_LIST_MODE:
                raise AnnotatorError("invalid_rule", f"list_meta.type 不支持: {list_type}", 400)
            list_roi_back = self._parse_roi_tuple(str(list_meta.get("roiBack", "")))

            if list_type == "image":
                template = self.get_list_item_image_file(task_name, json_relpath, item_name)
                from module.atom.image import RuleImage

                target = RuleImage(
                    roi_front=list_roi_back,
                    roi_back=list_roi_back,
                    method="Template matching",
                    threshold=0.8,
                    file=str(template),
                )
                detail = detect_image_detail(str(source_path), target)
                return {
                    "rule_type": "list",
                    "list_type": "image",
                    "item_name": item_name,
                    "image_name": self._list_item_image_name(item_name),
                    "matched": bool(detail.get("matched", False)),
                    "similarity": float(detail.get("similarity", 0.0)),
                    "roiFront": self._parse_roi(str(detail.get("roiFront", self._parse_roi(str(list_meta.get("roiBack", "0,0,100,100")))))),
                    "roiBack": self._parse_roi(str(detail.get("roiBack", self._parse_roi(str(list_meta.get("roiBack", "0,0,100,100")))))),
                    "threshold": 0.8,
                    "message": str(detail.get("message", "not_match")),
                }

            from module.atom.ocr import RuleOcr

            target = RuleOcr(
                name=item_name,
                mode="Full",
                method="Default",
                roi=list_roi_back,
                area=(0, 0, 10, 10),
                keyword=item_name,
            )
            detail = detect_ocr_detail(str(source_path), target)
            return {
                "rule_type": "list",
                "list_type": "ocr",
                "item_name": item_name,
                "matched": bool(detail.get("matched", False)),
                "similarity": float(detail.get("similarity", 0.0)),
                "text": str(detail.get("text", "")),
                "keyword": item_name,
                "roiFront": self._parse_roi(str(detail.get("roiFront", self._parse_roi(str(list_meta.get("roiBack", "0,0,100,100")))))),
                "roiBack": self._parse_roi(str(detail.get("roiBack", self._parse_roi(str(list_meta.get("roiBack", "0,0,100,100")))))),
                "message": str(detail.get("message", "not_match")),
            }

        raise AnnotatorError("invalid_rule_type", f"不支持测试的 rule_type: {rule_type}", 400)

    def save_rules_and_generate(
        self,
        session_id: str,
        task_name: str,
        json_relpath: str,
        rule_type: str,
        rules: list[dict[str, Any]],
        list_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = self._get_session(session_id)
        if not isinstance(rules, list):
            raise AnnotatorError("invalid_rule", "rules 必须是数组", 400)

        task_root, target_json = self._resolve_json_path(task_name, json_relpath)

        if rule_type == "image":
            payload = self._normalize_image_rules(rules) if rules else []
        elif rule_type == "ocr":
            payload = self._normalize_ocr_rules(rules) if rules else []
        elif rule_type == "click":
            payload = self._normalize_click_rules(rules) if rules else []
        elif rule_type == "swipe":
            payload = self._normalize_swipe_rules(rules) if rules else []
        elif rule_type == "long_click":
            payload = self._normalize_long_click_rules(rules) if rules else []
        elif rule_type == "list":
            payload = self._normalize_list_rules(rules, list_meta)
        else:
            raise AnnotatorError("invalid_rule_type", f"不支持的 rule_type: {rule_type}", 400)

        target_json.parent.mkdir(parents=True, exist_ok=True)

        lock = FileLock(f"{target_json}.lock")
        with lock:
            with atomic_write(target_json, overwrite=True, encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

        save_status = "success"
        generate_status = "success"
        generate_error = ""
        assets_file = ""
        extract_root = None
        try:
            extract_root, assets_path = self._resolve_assets_extract_target(task_root, target_json)
            assets_file = str(assets_path.relative_to(PROJECT_ROOT).as_posix())
            AssetsExtractor(str(extract_root)).extract()
        except Exception as e:
            generate_status = "failed"
            generate_error = str(e)
            logger.exception(
                f"[annotator] assets generate failed, session={session_id}, task={task_name}, "
                f"target={target_json}, extract_root={extract_root}"
            )

        logger.info(
            f"[annotator] save rules, session={session_id}, task={task_name}, target={target_json}, "
            f"rule_type={rule_type}, save_status={save_status}, generate_status={generate_status}, "
            f"assets_file={assets_file or 'n/a'}"
        )

        rule_count = len(payload.get("list", [])) if isinstance(payload, dict) and "list" in payload else len(payload)

        return {
            "save_status": save_status,
            "generate_status": generate_status,
            "error": generate_error,
            "target_json": str(target_json.relative_to(PROJECT_ROOT).as_posix()),
            "assets_file": assets_file,
            "rule_count": rule_count,
        }

    def save_cropped_image(
        self,
        session_id: str,
        image_id: str,
        task_name: str,
        json_relpath: str,
        image_name: str,
        roi: str,
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        image = session.images.get(image_id)
        if image is None:
            raise AnnotatorError("image_not_found", f"图片不存在: {image_id}", 404)

        if not image.path.exists():
            raise AnnotatorError("image_not_found", "源图片文件不存在", 404)

        task_root, target_json = self._resolve_json_path(task_name, json_relpath)
        image_rel = Path(image_name)
        if image_rel.is_absolute():
            raise AnnotatorError("invalid_path", "image_name 必须是相对路径", 400)

        target_image = (target_json.parent / image_rel).resolve()
        self._ensure_within_root(target_image, task_root)
        if target_image.suffix.lower() not in ALLOWED_IMAGE_EXT:
            raise AnnotatorError("invalid_image_ext", "目标图片后缀不受支持", 400)

        raw = cv2.imread(str(image.path), cv2.IMREAD_COLOR)
        if raw is None:
            raise AnnotatorError("invalid_image_data", "源图片读取失败", 400)

        roi_text = self._parse_roi(roi)
        x_f, y_f, w_f, h_f = [float(v) for v in roi_text.split(",")]
        x = int(round(x_f))
        y = int(round(y_f))
        w = int(round(w_f))
        h = int(round(h_f))

        ih, iw = raw.shape[:2]
        x = max(0, min(x, iw - 1))
        y = max(0, min(y, ih - 1))
        w = max(1, min(w, iw - x))
        h = max(1, min(h, ih - y))
        crop = raw[y:y + h, x:x + w]

        target_image.parent.mkdir(parents=True, exist_ok=True)
        ok = cv2.imwrite(str(target_image), crop)
        if not ok:
            raise AnnotatorError("save_image_failed", "保存裁剪图片失败", 500)

        logger.info(
            f"[annotator] save crop image, session={session_id}, image={image_id}, target={target_image}, roi={roi_text}"
        )

        return {
            "save_status": "success",
            "target_image": str(target_image.relative_to(PROJECT_ROOT).as_posix()),
            "roi": roi_text,
        }


annotator_manager = AnnotatorManager()
