# This Python file uses the following encoding: utf-8
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from module.log_income import (
    build_income_report,
    delete_income_days,
    income_dates,
    income_history,
)
from module.server.api_logger import ApiLoggingRoute

income_app = APIRouter(
    prefix="/income",
    tags=["income"],
    route_class=ApiLoggingRoute,
)


class IncomeItemResponse(BaseModel):
    """一项资源：当天读到多少、比上一次变化多少"""

    name: str
    value: int
    # 上一次没读到或者本次没读到时为 null，前端不要拿它当 0
    delta: int | None = None


class IncomeDayResponse(BaseModel):
    script_name: str
    date: str
    # 快照的采集时间，空字符串表示日志里没带时间戳
    collected_at: str = ''
    # 差值基准日期，空字符串表示没有可比的记录
    baseline_date: str = ''
    items: list[IncomeItemResponse] = Field(default_factory=list)


class IncomeDatesResponse(BaseModel):
    script_name: str
    dates: list[str] = Field(default_factory=list)


class IncomeRecordItem(BaseModel):
    """历史里的某一天"""

    date: str
    # 键是 INCOME_FIELDS 里的字段名，没读到的项是 -1
    values: dict[str, int] = Field(default_factory=dict)


class IncomeHistoryResponse(BaseModel):
    script_name: str
    days: int
    start: str
    end: str
    # 按日期升序；没跑任务的日子直接不出现，不要拿 0 去填
    records: list[IncomeRecordItem] = Field(default_factory=list)


class IncomeDeleteResponse(BaseModel):
    script_name: str
    deleted: list[str] = Field(default_factory=list)
    # 删完还剩几天
    remaining: int = 0


@income_app.get("/{script_name}/dates", response_model=IncomeDatesResponse)
async def income_available_dates(script_name: str):
    """有收益记录的日期，从新到旧"""
    return IncomeDatesResponse(
        script_name=script_name,
        dates=[day.isoformat() for day in income_dates(script_name)],
    )


@income_app.get("/{script_name}/history", response_model=IncomeHistoryResponse)
async def income_history_records(
    script_name: str,
    days: int = Query(default=30, ge=1, le=3650, description="最近多少天"),
):
    """最近若干天的收益记录，给前端画折线图"""
    return income_history(script_name, days)


@income_app.delete("/{script_name}/{date_text}", response_model=IncomeDeleteResponse)
async def income_delete_day(script_name: str, date_text: str):
    """删除某一天的收益记录"""
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid date format, expected YYYY-MM-DD",
        ) from exc

    if date_text not in {item.isoformat() for item in income_dates(script_name)}:
        raise HTTPException(
            status_code=404,
            detail=f"No income record on {date_text}",
        )

    return IncomeDeleteResponse(
        script_name=script_name,
        deleted=delete_income_days(script_name, [day]),
        remaining=len(income_dates(script_name)),
    )


@income_app.delete("/{script_name}", response_model=IncomeDeleteResponse)
async def income_delete_records(
    script_name: str,
    dates: str = Query(default="", description="逗号分隔的日期，留空表示全部"),
):
    """批量删除收益记录，dates 留空就是清空这个配置的全部记录"""
    wanted = [text.strip() for text in dates.split(',') if text.strip()]
    if wanted:
        try:
            days = [datetime.strptime(text, "%Y-%m-%d").date() for text in wanted]
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="Invalid date format, expected YYYY-MM-DD",
            ) from exc
    else:
        days = income_dates(script_name)

    return IncomeDeleteResponse(
        script_name=script_name,
        deleted=delete_income_days(script_name, days),
        remaining=len(income_dates(script_name)),
    )


@income_app.get("/{script_name}", response_model=IncomeDayResponse)
async def income_day(
    script_name: str,
    date_text: str = Query(..., alias="date", description="YYYY-MM-DD"),
):
    """某一天的收益快照与较上一次的变化"""
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid date format, expected YYYY-MM-DD",
        ) from exc

    report = build_income_report(script_name, day)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail=f"No income snapshot on {date_text}",
        )
    return report
