# This Python file uses the following encoding: utf-8
"""每日收益快照的读写，分两层存放。

不写进配置：配置会被"复制配置/导出配置"整份带到别的号上。按配置名分开存才天然隔离。

1. 日志行 `log/<日期>_<配置名>.txt`
   人可读，日志面板里能看到，排查问题方便。但日志只保留 7 天
   （module/logger.py::cleanup_logs），所以它同时是**旧数据的兜底**。

2. 日汇总 `log/income/<配置名>.jsonl`
   一天一行，读取时优先用它。放在 log/ 的子目录里所以不受日志清理影响——
   cleanup_logs 只递归 error 目录，其余子目录直接跳过。

任务侧负责写（两处都写），服务端负责读（日汇总优先，查不到再回退日志），
两边共用这里的解析，避免各写一份。

日志格式（字段顺序即 INCOME_FIELDS 的顺序）：
    Income snapshot: gold=14790000 jade=18832 ap=856 ...

注意 Rich 写文件日志时按 160 列折行，这一行通常会被切成几段，续段没有时间戳前缀。
"""

from __future__ import annotations

import json
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
# 日汇总目录。注意必须在 log/ 下且不是 error，这样 cleanup_logs 不会动它
INCOME_STORE_DIR = Path('./log/income')

# 记录开头的时间戳，用来给出"采集时间"
_RECORD_TIME = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
# 折行的续段没有时间戳，内容只剩 key=value
_CONTINUATION = re.compile(r'^[\w=]+(?: [\w=]+)*$')
# 日汇总文件名里要替换掉的危险字符
_STORE_NAME_UNSAFE = re.compile(r'[\\/:*?"<>|]')


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


def income_store_name(config_name: str) -> str:
    """日汇总用的配置名。

    不能沿用日志那套 normalize_log_name：它在第一个下划线处截断
    （大号_日常 -> 大号），而日汇总要长期保留，截断会让
    大号_日常 和 大号_活动 互相覆盖。这里只替换危险字符，保留完整名字。
    """
    name = _STORE_NAME_UNSAFE.sub('_', str(config_name or '').strip())
    name = name.replace('..', '_').strip(' .')
    return name or 'script'


def income_store_path(config_name: str) -> Path:
    """日汇总文件路径"""
    return INCOME_STORE_DIR / f'{income_store_name(config_name)}.jsonl'


def read_income_store(config_name: str) -> dict[str, dict]:
    """读日汇总，返回 {日期: 记录}。同一天写了多次时后面的覆盖前面的"""
    path = income_store_path(config_name)
    try:
        lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
    except OSError:
        return {}

    store: dict[str, dict] = {}
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
            key = str(entry['date'])
            values = {str(field): int(value) for field, value in entry['values'].items()}
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.warning(f'Income: skip broken store line in {path}')
            continue
        if set(values) != set(INCOME_FIELDS):
            logger.warning(f'Income: skip incomplete store line in {path}')
            continue
        store[key] = {'dt': str(entry.get('dt', '')), 'values': values}
    return store


def write_income_day(config_name: str, day: date, record: dict) -> None:
    """把某天的记录写进日汇总，同一天覆盖，保持一天一行"""
    path = income_store_path(config_name)
    key = day.isoformat()

    kept = []
    try:
        for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                # 坏行原样留着，不去猜也不擅自删
                if json.loads(text).get('date') == key:
                    continue
            except (ValueError, AttributeError):
                pass
            kept.append(text)
    except OSError:
        pass

    kept.append(json.dumps(
        {'date': key, 'dt': record.get('dt', ''), 'values': record['values']},
        ensure_ascii=False,
        sort_keys=True,
    ))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(kept) + '\n', encoding='utf-8')
    except OSError as e:
        logger.warning(f'Income: write {path} failed: {e}')


def _log_income_dates(config_name: str) -> list[date]:
    """从日志里能看到的日期。

    只按文件名匹配不够：那天可能跑过任务但没开收益统计，或者快照行写坏了，
    所以要逐天验一遍，免得下拉里出现点进去就 404 的死日期。
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
    return days


def income_dates(config_name: str) -> list[date]:
    """列出**真正有收益快照**的日期，从新到旧。

    日汇总（长存）与日志（约 7 天）取并集，这样本次改动之前只存在于日志里的
    历史不会立刻消失。
    """
    days = set()
    for key in read_income_store(config_name):
        try:
            days.add(date.fromisoformat(key))
        except ValueError:
            continue
    days.update(_log_income_dates(config_name))
    return sorted(days, reverse=True)


def load_income_day(config_name: str, day: date) -> Optional[dict]:
    """读某一天的快照：日汇总优先，没有再回退到当天的日志"""
    stored = read_income_store(config_name).get(day.isoformat())
    if stored is not None:
        return stored
    return last_income_record(income_day_path(config_name, day))


def find_income_baseline(config_name: str, before: date) -> Optional[tuple[date, dict]]:
    """找最近一个有快照、且早于 before 的日子。

    逐日往前找而不是只认前一天，这样机器停几天再开机也能算出跨天的收益。
    """
    store = read_income_store(config_name)
    for day in income_dates(config_name):
        if day >= before:
            continue
        record = store.get(day.isoformat()) or last_income_record(
            income_day_path(config_name, day))
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
