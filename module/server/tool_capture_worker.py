# This Python file uses the following encoding: utf-8
import time
from queue import Empty, Full
from typing import Any

import cv2

from module.config.config import Config
from module.device.device import Device
from module.logger import logger

EMULATOR_CAPTURE_MAX_RETRIES = 3
EMULATOR_CAPTURE_RETRY_BACKOFF_SECONDS = 1.0


def _build_device(session_id: str, config_name: str, config: Config, interval: float) -> Device:
    device = Device(config=config)
    device.disable_stuck_detection()
    device.screenshot_interval_set(interval)
    logger.info(
        f"[annotator] emulator device ready, session={session_id}, "
        f"config={config_name}, interval={interval:.3f}"
    )
    return device


def _release_device(device: Device | None) -> None:
    if device is None:
        return
    try:
        device.release_during_wait()
    except Exception as e:
        # 这里以前是静默 pass，导致设备侧清理失败完全无痕。
        # 注意 release_during_wait() 会读 config.script.device.screenshot_method，
        # 配置里没有 script.device 时该链路会抛 AttributeError（Config.__getattr__ 返回 None）。
        logger.warning(f"[annotator] release device failed: {type(e).__name__}: {e}")


def _format_error_message(error: Exception) -> str:
    text = str(error).strip()
    if text:
        return text
    return error.__class__.__name__


def _put_state(queue, payload: dict[str, Any]) -> None:
    try:
        queue.put_nowait(payload)
    except Full:
        try:
            queue.get_nowait()
        except Empty:
            pass
        try:
            queue.put_nowait(payload)
        except Full:
            pass


def _put_latest_frame(queue, jpeg: bytes, updated_at: float) -> None:
    payload = {"jpeg": jpeg, "updated_at": updated_at}
    while True:
        try:
            queue.get_nowait()
        except Empty:
            break
    try:
        queue.put_nowait(payload)
    except Full:
        pass


def _put_response(queue, request_id: str, ok: bool, code: str = "", message: str = "") -> None:
    try:
        queue.put_nowait(
            {
                "request_id": request_id,
                "ok": ok,
                "code": code,
                "message": message,
            }
        )
    except Full:
        pass


def _handle_commands(command_queue, response_queue, latest_frame) -> bool:
    stop_requested = False
    while True:
        try:
            command = command_queue.get_nowait()
        except Empty:
            break

        command_type = str(command.get("type", "")).strip()
        if command_type == "stop":
            stop_requested = True
            continue

        if command_type != "capture":
            continue

        request_id = str(command.get("request_id", "")).strip()
        output_file = str(command.get("output_file", "")).strip()
        if not request_id or not output_file:
            continue

        if latest_frame is None:
            _put_response(response_queue, request_id, False, "no_frame", "当前没有可用帧，无法截图")
            continue

        ok = cv2.imwrite(output_file, latest_frame)
        if ok:
            _put_response(response_queue, request_id, True)
        else:
            _put_response(response_queue, request_id, False, "capture_failed", "保存截图失败")
    return stop_requested


def _sleep_with_commands(seconds: float, command_queue, response_queue, latest_frame) -> bool:
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if _handle_commands(command_queue, response_queue, latest_frame):
            return True
        time.sleep(min(0.05, max(0.0, deadline - time.time())))
    return _handle_commands(command_queue, response_queue, latest_frame)


def run_annotator_capture_worker(
    session_id: str,
    config_name: str,
    frame_rate: int,
    state_queue,
    frame_queue,
    command_queue,
    response_queue,
) -> None:
    device: Device | None = None
    latest_frame = None
    retry_count = 0
    max_retries = EMULATOR_CAPTURE_MAX_RETRIES
    interval = max(0.1, 1.0 / float(frame_rate))
    final_state = "stopped"
    final_error = ""
    final_error_at = 0.0

    _put_state(
        state_queue,
        {
            "state": "starting",
            "error": "",
            "retry_count": 0,
            "max_retries": max_retries,
            "last_error_at": 0.0,
        },
    )

    try:
        config = Config(config_name=config_name)
        while True:
            if _handle_commands(command_queue, response_queue, latest_frame):
                break

            if device is None:
                try:
                    device = _build_device(session_id, config_name, config, interval)
                    _put_state(
                        state_queue,
                        {
                            "state": "running",
                            "error": "",
                            "retry_count": retry_count,
                            "max_retries": max_retries,
                            "last_error_at": final_error_at,
                        },
                    )
                except Exception as error:
                    message = _format_error_message(error)
                    retry_count += 1
                    final_error = message
                    final_error_at = time.time()
                    should_retry = retry_count <= max_retries
                    _put_state(
                        state_queue,
                        {
                            "state": "starting" if should_retry else "error",
                            "error": message,
                            "retry_count": retry_count,
                            "max_retries": max_retries,
                            "last_error_at": final_error_at,
                        },
                    )
                    if should_retry:
                        logger.warning(
                            f"[annotator] emulator capture retry, session={session_id}, config={config_name}, "
                            f"stage=connect, attempt={retry_count}/{max_retries}, error={message}"
                        )
                        if _sleep_with_commands(
                            EMULATOR_CAPTURE_RETRY_BACKOFF_SECONDS * retry_count,
                            command_queue,
                            response_queue,
                            latest_frame,
                        ):
                            break
                        continue

                    logger.error(
                        f"[annotator] emulator capture failed, session={session_id}, config={config_name}, "
                        f"stage=connect, attempt={retry_count}/{max_retries}, error={message}"
                    )
                    final_state = "error"
                    break

            try:
                frame = device.screenshot()
            except Exception as error:
                _release_device(device)
                device = None
                message = _format_error_message(error)
                retry_count += 1
                final_error = message
                final_error_at = time.time()
                should_retry = retry_count <= max_retries
                _put_state(
                    state_queue,
                    {
                        "state": "starting" if should_retry else "error",
                        "error": message,
                        "retry_count": retry_count,
                        "max_retries": max_retries,
                        "last_error_at": final_error_at,
                    },
                )
                if should_retry:
                    logger.warning(
                        f"[annotator] emulator capture retry, session={session_id}, config={config_name}, "
                        f"stage=capture, attempt={retry_count}/{max_retries}, error={message}"
                    )
                    if _sleep_with_commands(
                        EMULATOR_CAPTURE_RETRY_BACKOFF_SECONDS * retry_count,
                        command_queue,
                        response_queue,
                        latest_frame,
                    ):
                        break
                    continue

                logger.error(
                    f"[annotator] emulator capture failed, session={session_id}, config={config_name}, "
                    f"stage=capture, attempt={retry_count}/{max_retries}, error={message}"
                )
                final_state = "error"
                break

            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            latest_frame = frame_bgr.copy()
            ok, buf = cv2.imencode(".jpg", frame_bgr)
            if ok:
                _put_latest_frame(frame_queue, buf.tobytes(), time.time())
                retry_count = 0
                _put_state(
                    state_queue,
                    {
                        "state": "running",
                        "error": "",
                        "retry_count": 0,
                        "max_retries": max_retries,
                        "last_error_at": final_error_at,
                    },
                )

            if _sleep_with_commands(interval, command_queue, response_queue, latest_frame):
                break
    except Exception:
        final_state = "error"
        if not final_error:
            final_error = "模拟器采集进程异常退出"
            final_error_at = time.time()
        logger.exception(
            f"[annotator] emulator capture loop crashed, session={session_id}, config={config_name}"
        )
    finally:
        _release_device(device)
        _handle_commands(command_queue, response_queue, latest_frame)
        if final_state == "error":
            _put_state(
                state_queue,
                {
                    "state": "error",
                    "error": final_error or "模拟器采集进程异常退出",
                    "retry_count": retry_count,
                    "max_retries": max_retries,
                    "last_error_at": final_error_at or time.time(),
                },
            )
        else:
            _put_state(
                state_queue,
                {
                    "state": "stopped",
                    "error": "",
                    "retry_count": 0,
                    "max_retries": max_retries,
                    "last_error_at": final_error_at,
                },
            )
