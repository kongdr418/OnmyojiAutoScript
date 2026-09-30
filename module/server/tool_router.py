# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketState

from module.logger import logger
from module.server.api_logger import ApiLoggingRoute, log_ws_event
from module.server.tool import AnnotatorError, annotator_manager

tool_app = APIRouter(
    prefix="/tool",
    tags=["tool"],
    route_class=ApiLoggingRoute,
)

# 单帧推送的硬超时。
# websockets 的 drain() 会在发送缓冲区写满且对端不收时一直 await；
# 客户端异常断开（半开连接）时 TCP 可能长时间不报错，于是整个事件循环被卡死，
# 表现为服务进程还在、端口能连、但所有 HTTP 请求超时。
# 给它加超时，宁可丢弃这一帧也不能阻塞事件循环。
ANNOTATOR_WS_SEND_TIMEOUT_SECONDS = 2.0

# 用 receive() 的短超时来探测客户端断开；超时即“暂无消息”，属正常情况
ANNOTATOR_WS_POLL_SECONDS = 0.1


def _is_ws_connected(websocket: WebSocket) -> bool:
    """WebSocket 是否仍处于可发送状态。"""
    return websocket.client_state == WebSocketState.CONNECTED


async def _ws_client_gone(websocket: WebSocket) -> bool:
    """探测客户端是否已断开。

    正常收帧期间不会有客户端消息，超时即视为“仍然在线”。
    """
    try:
        await asyncio.wait_for(websocket.receive(), timeout=ANNOTATOR_WS_POLL_SECONDS)
        # 收到任何消息都视为客户端主动结束（本端点是单向推流）
        return True
    except asyncio.TimeoutError:
        return False
    except WebSocketDisconnect:
        return True
    except RuntimeError:
        # 状态已切换到 DISCONNECTED 之类
        return True


async def _ws_send_frame(websocket: WebSocket, frame: bytes) -> bool:
    """带超时地推送一帧；返回 False 表示客户端已不可用，应结束推流。"""
    if not _is_ws_connected(websocket):
        return False
    try:
        await asyncio.wait_for(websocket.send_bytes(frame), timeout=ANNOTATOR_WS_SEND_TIMEOUT_SECONDS)
        return True
    except asyncio.TimeoutError:
        logger.warning(
            f"[annotator] ws send timeout after {ANNOTATOR_WS_SEND_TIMEOUT_SECONDS}s, "
            f"drop frame and close stream"
        )
        return False
    except (WebSocketDisconnect, RuntimeError):
        return False
    except Exception as e:
        # 其它异常（例如 websockets 的 ConnectionClosedError）同样视为客户端已走
        if _looks_like_client_disconnect(e):
            return False
        raise


async def _ws_send_json(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """带超时地推送一条 JSON 事件；返回 False 表示客户端已不可用。"""
    if not _is_ws_connected(websocket):
        return False
    try:
        await asyncio.wait_for(websocket.send_json(payload), timeout=ANNOTATOR_WS_SEND_TIMEOUT_SECONDS)
        return True
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        return False
    except Exception as e:
        if _looks_like_client_disconnect(e):
            return False
        raise


def _looks_like_client_disconnect(error: BaseException) -> bool:
    """判断异常是否属于“客户端已断开”而非服务端真错误。

    websockets 库在连接被对端异常关闭时抛 ConnectionClosedError，
    其类名叫 ConnectionClosed、消息为 "no close frame received or sent"，
    既不是 WebSocketDisconnect 也不含 "disconnect" 字样，需要显式识别。
    """
    name = error.__class__.__name__
    if name in {"ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError", "ClientDisconnected"}:
        return True
    if isinstance(error, WebSocketDisconnect):
        return True
    message = str(error).strip().lower()
    return any(
        token in message
        for token in ("no close frame", "connection is closed", "connection closed", "disconnect")
    )


class EmulatorStartBody(BaseModel):
    session_id: str
    config_name: str
    frame_rate: int = 2


class SessionBody(BaseModel):
    session_id: str


class RuleSaveBody(BaseModel):
    session_id: str
    task_name: str
    json_relpath: str
    rule_type: str
    rules: list[dict[str, Any]]
    list_meta: dict[str, Any] | None = None


class UploadImageItem(BaseModel):
    name: str
    content_base64: str


class UploadImagesBody(BaseModel):
    session_id: str
    images: list[UploadImageItem]


class BatchDeleteImagesBody(BaseModel):
    session_id: str
    image_ids: list[str]


class CropSaveBody(BaseModel):
    session_id: str
    image_id: str
    task_name: str
    json_relpath: str
    image_name: str
    roi: str


class RuleImageDeleteBody(BaseModel):
    task_name: str
    json_relpath: str
    image_name: str


class RuleFileCreateBody(BaseModel):
    dir_path: str
    file_name: str


class RuleFileDeleteBody(BaseModel):
    dir_path: str
    file_name: str


class RuleTestBody(BaseModel):
    session_id: str
    image_id: str
    task_name: str
    json_relpath: str
    rule_type: str
    rule: dict[str, Any]
    list_meta: dict[str, Any] | None = None

def _raise_annotator_error(e: AnnotatorError) -> None:
    raise HTTPException(
        status_code=e.status_code,
        detail={"code": e.code, "message": e.message},
    )


def _close_session_safely(session_id: str, reason: str) -> dict[str, Any]:
    return annotator_manager.close_session(session_id, reason=reason, raise_if_missing=False)


@tool_app.get('/annotator')
async def tool_annotator_page():
    page = annotator_manager.index_file()
    if not page.exists():
        raise HTTPException(status_code=404, detail={"code": "page_not_found", "message": "标注页面不存在"})
    return FileResponse(page)


@tool_app.post('/annotator/api/session')
async def annotator_create_session():
    session = annotator_manager.create_session()
    return {"code": "ok", "session": session}


@tool_app.get('/annotator/api/session/{session_id}')
async def annotator_get_session(session_id: str):
    try:
        session = annotator_manager.get_session_snapshot(session_id)
        return {"code": "ok", "session": session}
    except AnnotatorError as e:
        _raise_annotator_error(e)




@tool_app.delete('/annotator/api/session/{session_id}')
async def annotator_close_session(session_id: str, reason: str = "client_close"):
    try:
        # 关会话内部会停采集进程（同步 join/terminate），同样不能占用事件循环
        result = await run_in_threadpool(_close_session_safely, session_id, f"api:{reason}")
        return {"code": "ok", **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/session/{session_id}/close')
async def annotator_close_session_beacon(session_id: str, reason: str = "pagehide"):
    try:
        # 关会话内部会停采集进程（同步 join/terminate），同样不能占用事件循环
        result = await run_in_threadpool(_close_session_safely, session_id, f"beacon:{reason}")
        return {"code": "ok", **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)

@tool_app.post('/annotator/api/images/upload')
async def annotator_upload_images(data: UploadImagesBody):
    try:
        images = annotator_manager.save_uploaded_images_base64(
            data.session_id,
            [item.dict() for item in data.images],
        )
        return {"code": "ok", "images": images}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.get('/annotator/api/images')
async def annotator_list_images(session_id: str):
    try:
        images = annotator_manager.list_images(session_id)
        return {"code": "ok", "images": images}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.get('/annotator/api/images/{session_id}/{image_id}')
async def annotator_image_file(session_id: str, image_id: str):
    try:
        image = annotator_manager.get_image_file(session_id, image_id)
        return FileResponse(image)
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.delete('/annotator/api/images/{session_id}/{image_id}')
async def annotator_delete_image(session_id: str, image_id: str):
    try:
        session = annotator_manager.delete_image(session_id, image_id)
        return {"code": "ok", "session": session}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/images/delete-batch')
async def annotator_delete_batch_images(data: BatchDeleteImagesBody):
    try:
        result = annotator_manager.delete_images(data.session_id, data.image_ids)
        return {"code": "ok", **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/images/clear')
async def annotator_clear_images(data: SessionBody):
    try:
        result = annotator_manager.clear_images(data.session_id)
        return {"code": "ok", **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.get('/annotator/api/configs')
async def annotator_configs():
    configs = annotator_manager.list_configs()
    return {"code": "ok", "configs": configs}


@tool_app.get('/annotator/api/tasks')
async def annotator_tasks():
    tasks = annotator_manager.list_task_names()
    return {"code": "ok", "tasks": tasks}


@tool_app.get('/annotator/api/tasks/{task_name}/json')
async def annotator_task_json_files(task_name: str):
    try:
        files = annotator_manager.list_task_json_files(task_name)
        return {"code": "ok", "json_files": files}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.get('/annotator/api/rules/schema')
async def annotator_rule_schema():
    try:
        data = annotator_manager.rule_schema()
        return {"code": "ok", **data}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.get('/annotator/api/rules/load')
async def annotator_load_rules(task_name: str, json_relpath: str):
    try:
        data = annotator_manager.load_rule_file(task_name, json_relpath)
        return {"code": "ok", **data}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.get('/annotator/api/rules/source')
async def annotator_rule_source(dir_path: str = ""):
    try:
        data = annotator_manager.list_rule_source(dir_path)
        return {"code": "ok", **data}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/rules/source/create')
async def annotator_rule_source_create(data: RuleFileCreateBody):
    try:
        result = annotator_manager.create_rule_json(data.dir_path, data.file_name)
        return {"code": "ok", **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/rules/source/delete')
async def annotator_rule_source_delete(data: RuleFileDeleteBody):
    try:
        result = annotator_manager.delete_rule_json(data.dir_path, data.file_name)
        return {"code": "ok", **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.get('/annotator/api/rules/image-preview')
async def annotator_rule_image_preview(task_name: str, json_relpath: str, image_name: str):
    try:
        image = annotator_manager.get_rule_image_file(task_name, json_relpath, image_name)
        return FileResponse(image)
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/rules/image/delete')
async def annotator_rule_image_delete(data: RuleImageDeleteBody):
    try:
        result = annotator_manager.delete_rule_image(data.task_name, data.json_relpath, data.image_name)
        return {"code": "ok", **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/emulator/start')
async def annotator_start_emulator(data: EmulatorStartBody):
    try:
        # start_emulator() 内部会先 stop() 掉旧采集进程（同步 join/terminate），
        # 同样可能阻塞数秒，必须丢到线程池。
        status = await run_in_threadpool(
            annotator_manager.start_emulator,
            data.session_id,
            data.config_name,
            data.frame_rate,
        )
        return {"code": "ok", "emulator": status}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/emulator/stop')
async def annotator_stop_emulator(data: SessionBody):
    try:
        # stop_emulator() 会同步 join/terminate 子进程（对远端设备最多等 8s），
        # 必须丢到线程池，否则会阻塞事件循环导致整个服务无响应。
        status = await run_in_threadpool(annotator_manager.stop_emulator, data.session_id)
        return {"code": "ok", "emulator": status}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.get('/annotator/api/emulator/status')
async def annotator_emulator_status(session_id: str):
    try:
        status = annotator_manager.emulator_status(session_id)
        return {"code": "ok", "emulator": status}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/emulator/capture')
async def annotator_capture_frame(data: SessionBody):
    try:
        # capture_from_emulator() 会同步等待采集进程回执，最长 5s
        image = await run_in_threadpool(annotator_manager.capture_from_emulator, data.session_id)
        return {"code": "ok", "image": image}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/rules/test')
async def annotator_test_rule(data: RuleTestBody):
    try:
        result = annotator_manager.test_rule(
            session_id=data.session_id,
            image_id=data.image_id,
            task_name=data.task_name,
            json_relpath=data.json_relpath,
            rule_type=data.rule_type,
            rule=data.rule,
            list_meta=data.list_meta,
        )
        return {"code": "ok", "result": result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/rules/save')
async def annotator_save_rules(data: RuleSaveBody):
    try:
        result = annotator_manager.save_rules_and_generate(
            session_id=data.session_id,
            task_name=data.task_name,
            json_relpath=data.json_relpath,
            rule_type=data.rule_type,
            rules=data.rules,
            list_meta=data.list_meta,
        )
        code = "ok" if result.get("generate_status") == "success" else "partial_success"
        return {"code": code, **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.post('/annotator/api/images/crop-save')
async def annotator_crop_save(data: CropSaveBody):
    try:
        result = annotator_manager.save_cropped_image(
            session_id=data.session_id,
            image_id=data.image_id,
            task_name=data.task_name,
            json_relpath=data.json_relpath,
            image_name=data.image_name,
            roi=data.roi,
        )
        return {"code": "ok", **result}
    except AnnotatorError as e:
        _raise_annotator_error(e)


@tool_app.websocket('/annotator/ws/{session_id}')
async def annotator_frame_ws(websocket: WebSocket, session_id: str):
    await websocket.accept()
    log_ws_event(f"annotator_ws[{session_id}] connect")
    try:
        annotator_manager.get_session_snapshot(session_id)
        while True:
            # 客户端异常断开时 TCP 可能长时间不报错，必须先主动探测，
            # 否则会一直往没人收的 socket 写，最终 drain() 阻塞整个事件循环。
            if await _ws_client_gone(websocket):
                log_ws_event(f"annotator_ws[{session_id}] disconnect")
                logger.info(f"[annotator] ws client gone, session={session_id}")
                break

            frame = annotator_manager.latest_emulator_frame(session_id)
            if frame:
                if not await _ws_send_frame(websocket, frame):
                    log_ws_event(f"annotator_ws[{session_id}] disconnect")
                    logger.info(f"[annotator] ws stream stopped, session={session_id}")
                    break
                continue

            status = annotator_manager.emulator_status(session_id)
            if status.get("state") == "error":
                log_ws_event(f"annotator_ws[{session_id}] event: emulator_error")
                await _ws_send_json(
                    websocket,
                    {
                        "event": "error",
                        "code": "emulator_error",
                        "message": status.get("error", "unknown"),
                    },
                )
                try:
                    await websocket.close(code=1011)
                except Exception:
                    pass
                break
    except WebSocketDisconnect:
        log_ws_event(f"annotator_ws[{session_id}] disconnect")
        logger.info(f"[annotator] ws disconnect, session={session_id}")
    except AnnotatorError as e:
        log_ws_event(f"annotator_ws[{session_id}] annotator_error: code={e.code}, message={e.message}", level="warning")
        if e.code != "invalid_session":
            logger.warning(f"[annotator] ws annotator error, session={session_id}, code={e.code}")
        await _ws_send_json(websocket, {"event": "error", "code": e.code, "message": e.message})
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
    except Exception as e:
        if _looks_like_client_disconnect(e):
            # 客户端断开是常态，不该按 ERROR 打整段 traceback 淹掉日志
            log_ws_event(f"annotator_ws[{session_id}] client_disconnected_during_send")
            logger.info(f"[annotator] ws client disconnected, session={session_id}: {type(e).__name__}")
        else:
            log_ws_event(f"annotator_ws[{session_id}] error: {type(e).__name__}: {e}", level="error")
            logger.exception(f"[annotator] ws failed, session={session_id}")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if websocket.client_state == WebSocketState.DISCONNECTED:
            log_ws_event(f"annotator_ws[{session_id}] disconnect")
