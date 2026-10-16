# This Python file uses the following encoding: utf-8
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from module.log_income import build_income_report, income_dates
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


@income_app.get("/{script_name}/dates", response_model=IncomeDatesResponse)
async def income_available_dates(script_name: str):
    """有收益记录的日期，从新到旧（受日志保留期限制，当前是 7 天）"""
    return IncomeDatesResponse(
        script_name=script_name,
        dates=[day.isoformat() for day in income_dates(script_name)],
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
