"""Drawing queries return display data only. SDK authorization stays local."""
import asyncio

from fastapi import APIRouter, HTTPException
from filelock import Timeout
from pydantic import BaseModel, Field

from module.scrolls import service
from module.server.api_logger import ApiLoggingRoute


scrolls_app = APIRouter(prefix='/scrolls', tags=['scrolls'], route_class=ApiLoggingRoute)


async def run_operation(operation, *args):
    try:
        return await asyncio.to_thread(operation, *args)
    except service.ScrollError as error:
        raise HTTPException(error.status, detail=error.message) from error
    except Timeout as error:
        raise HTTPException(409, detail='账号正在读取或查询，请稍后重试') from error
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(503, detail='本地账号读取或网络连接失败，请检查设备连接、文件权限和网络') from error


@scrolls_app.get('/{script_name}')
async def scrolls_info(script_name: str):
    return await run_operation(service.info, script_name)


@scrolls_app.post('/{script_name}/query')
async def scrolls_query(script_name: str):
    return await run_operation(service.query, script_name)


@scrolls_app.post('/{script_name}/accounts/import')
async def scrolls_import_accounts(script_name: str):
    return await run_operation(service.import_accounts, script_name)


class AccountSelection(BaseModel):
    account_id: str = Field(max_length=64)


@scrolls_app.put('/{script_name}/account')
async def scrolls_select_account(script_name: str, selection: AccountSelection):
    return await run_operation(service.select_account, script_name, selection.account_id)
