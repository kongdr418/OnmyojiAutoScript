# This Python file uses the following encoding: utf-8
"""每日收益快照的日志读写。

快照以一行固定格式的日志存在 `log/<日期>_<配置名>.txt` 里，而不是写进配置：
配置会被"复制配置/导出配置"整份带到别的号上，日志是按配置名分文件的，天然隔离。

任务侧负责写，服务端负责读，两边共用这里的解析，避免各写一份。

日志格式（字段顺序即 INCOME_FIELDS 的顺序）：
    Income snapshot: gold=14790000 jade=18832 ap=856 ...

注意 Rich 写文件日志时按 160 列折行，这一行通常会被切成几段，续段没有时间戳前缀。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

from module.logger import logger, normalize_log_name

# 快照字段，顺序即日志里的顺序；改动时必须同步 tasks/DailyTrifles/config.py 的 IncomeSnapshot
INCOME_FIELDS = (
    'gold',
    'jade',
    'ap',
    'sca_orochi_scales',
    'sca_reverse_scale',
    'sca_demon_souls',
    'sca_picture_book',
    'mystery_amulet',
)

INCOME_LOG_PREFIX = 'Income snapshot: '
INCOME_LOG_DIR = Path('./log')

# 记录开头的时间戳，用来给出"采集时间"
_RECORD_TIME = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
# 折行的续段没有时间戳，内容只剩 key=value
_CONTINUATION = re.compile(r'^[\w=]+(?: [\w=]+)*$')


def parse_income_record(line: str) -> Optional[dict]:
    """解析一条快照记录，返回 {'dt': 采集时间, 'values': {字段: 数值}}。

    字段必须一个不少、值必须是整数，否则整行作废，不让半截数据参与做差。
    """
    index = line.find(INCOME_LOG_PREFIX)
    if index < 0:
        return None
    values: dict[str, int] = {}
    for token in line[index + len(INCOME_LOG_PREFIX):].split():
        key, _, text = token.partition('=')
        try:
            values[key] = int(text)
        except ValueError:
            return None
    if set(values) != set(INCOME_FIELDS):
        return None
    match = _RECORD_TIME.match(line)
    return {
        'dt': match.group(1) if match else '',
        'values': values,
    }


def last_income_record(path: Path) -> Optional[dict]:
    """取某天日志里最后一条快照，也就是当天的最终值"""
    try:
        lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
    except OSError as e:
        logger.warning(f'Income: read {path} failed: {e}')
        return None

    records: list[str] = []
    for line in lines:
        text = line.strip()
        # 只把"看着就是 key=value"的无时间戳行当折行续段；
        # logger.hr() 那种分隔线也没有时间戳，当成独立记录丢掉
        if records and INCOME_LOG_PREFIX in records[-1] and _CONTINUATION.match(text):
            records[-1] += ' ' + text
        else:
            records.append(text)

    for record in reversed(records):
        if INCOME_LOG_PREFIX not in record:
            continue
        parsed = parse_income_record(record)
        if parsed is not None:
            return parsed
        logger.warning(f'Income: skip broken snapshot line in {path}')
    return None


def income_day_path(config_name: str, day: date) -> Path:
    """日志文件名里的配置名是规范化过的，必须用同一套规则拼"""
    return INCOME_LOG_DIR / f'{day.isoformat()}_{normalize_log_name(config_name)}.txt'


def income_dates(config_name: str) -> list[date]:
    """列出**真正有收益快照**的日期，从新到旧。

    只按文件名匹配是不够的：那天可能跑过任务但没开收益统计，或者快照行写坏了。
    这里逐天验一遍，免得前端下拉里出现点进去就 404 的死日期。
    """
    if not INCOME_LOG_DIR.is_dir():
        return []
    suffix = f'_{normalize_log_name(config_name)}.txt'
    days = []
    for path in INCOME_LOG_DIR.iterdir():
        if not path.is_file() or not path.name.endswith(suffix):
            continue
        try:
            day = date.fromisoformat(path.name[:10])
        except ValueError:
            continue
        if last_income_record(path) is not None:
            days.append(day)
    return sorted(days, reverse=True)


def load_income_day(config_name: str, day: date) -> Optional[dict]:
    """读某一天的快照，没有快照返回 None"""
    return last_income_record(income_day_path(config_name, day))


def find_income_baseline(config_name: str, before: date) -> Optional[tuple[date, dict]]:
    """找最近一个有快照、且早于 before 的日子。

    逐日往前找而不是只认前一天，这样机器停几天再开机也能算出跨天的收益。
    """
    for day in income_dates(config_name):
        if day >= before:
            continue
        record = load_income_day(config_name, day)
        if record is not None:
            return day, record
    return None


def build_income_report(config_name: str, day: date) -> Optional[dict]:
    """某天的收益报告：当天快照的各项值 + 与上一个有记录日期的逐项差值"""
    record = load_income_day(config_name, day)
    if record is None:
        return None

    baseline = find_income_baseline(config_name, day)
    baseline_day, baseline_record = baseline if baseline else (None, None)
    baseline_values = baseline_record['values'] if baseline_record else {}

    items = []
    for field in INCOME_FIELDS:
        value = record['values'].get(field, -1)
        before = baseline_values.get(field, -1)
        # -1 表示当时没读到，不能拿它算差值
        delta = value - before if value >= 0 and before >= 0 else None
        items.append({'name': field, 'value': value, 'delta': delta})

    return {
        'script_name': config_name,
        'date': day.isoformat(),
        'collected_at': record['dt'],
        'baseline_date': baseline_day.isoformat() if baseline_day else '',
        'items': items,
    }
