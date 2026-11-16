"""Select live-memory or independent queries for one OAS configuration."""

from contextlib import contextmanager
from time import perf_counter

from filelock import Timeout
from module.logger import logger

from module.scrolls.client import fragment_counts, now_string, progress_rows, query_details
from module.scrolls.activity import current_activity
from module.scrolls.device import ScrollDevice
from module.scrolls.mumu import read_game_memory
from module.scrolls.live_objects import LiveRoleMismatch, LiveCacheUnavailable
from module.scrolls.saved_accounts import read_accounts
from module.scrolls.servers import gate_address
from module.scrolls.rank_cache import enrich_rankings
from module.scrolls.storage import CredentialCatalog, ScrollStore, account_lock, binding, device_lock, selection_lock


class ScrollError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@contextmanager
def query_stage(name, stage):
    started = perf_counter()
    try:
        yield
    finally:
        logger.info(f'绘卷查询 [{name}] {stage} 耗时={perf_counter() - started:.3f}s')


def configuration(name):
    from module.server.config_manager import ConfigManager
    try:
        name = ConfigManager.validate_config_name(name, allow_template=False)
    except ValueError as error:
        raise ScrollError('invalid_config', '配置名称无效') from error
    path = ConfigManager.config_path(name)
    if not path.is_file():
        raise ScrollError('config_missing', '配置不存在', 404)
    from module.config.config import Config
    return name, Config(name)


def _source_valid(record):
    try:
        _, config = configuration(record['source_script'])
        return record.get('binding') == binding(config)
    except (ScrollError, KeyError, ValueError, RuntimeError, OSError):
        return False


def info(name, activity=None):
    name, device = configuration(name)
    store = ScrollStore(name)
    selected = store.selection()
    cached = store.result()
    if cached and (cached.get('_binding') != binding(device) or cached.get('_credential', '') != selected):
        cached = None
    if cached:
        activity = activity or current_activity()
        if not activity.matches(cached):
            cached = None
    authorized = False
    message = ''
    try:
        record = store.read()
        authorized = bool(record and record.get('binding') == binding(device))
    except (RuntimeError, ValueError, OSError):
        message = '本地授权无法读取，请在游戏登录后重新查询以获取授权'
    try:
        catalog = CredentialCatalog().read()
    except (RuntimeError, ValueError, OSError):
        catalog = {}
        message = '本地账号列表无法读取，请重新读取本地账号'
    accounts = [{'id': identifier, 'identity': record['identity'], 'source_script': record['source_script'],
                 'instance': record.get('instance', ''), 'available': _source_valid(record)}
                for identifier, record in catalog.items() if record.get('source_script') == name]
    accounts.sort(key=lambda item: (item['source_script'], item['identity'].get('server_id', ''),
                                    item['identity'].get('name', ''), item['id']))
    if selected:
        selected_record = catalog.get(selected)
        authorized = bool(selected_record and selected_record.get('source_script') == name
                          and _source_valid(selected_record))
        message = '' if authorized else '请重新选择当前配置的查询账号'
        if not selected_record or selected_record.get('source_script') != name:
            cached = None
        elif cached and (cached.get('identity', {}).get('role_id') != selected_record['account'].get('user_id')
                         or str(cached.get('identity', {}).get('server_id')) != str(selected_record['account'].get('serverid'))):
            cached = None
    if cached:
        # Backfill rankings without changing the saved progress collection time
        # or opening a game session. The cache is exact-server/current-issue scoped.
        cached = enrich_rankings(cached, activity)
    return {
        'script_name': name, 'authorized': authorized,
        'result': {key: value for key, value in cached.items() if not key.startswith('_')} if cached else None,
        'message': message, 'accounts': accounts, 'selected_account_id': selected,
    }


def import_accounts(name):
    name, settings = configuration(name)
    with selection_lock(name).acquire(timeout=0):
        device = ScrollDevice(settings)
        with device_lock(device.serial).acquire(timeout=0):
            records = read_accounts(device)
            records = [{**record, 'device_type': getattr(device.instance, 'type', '')} for record in records]
            CredentialCatalog().replace_source(name, binding(settings), device.instance.name, device.serial, records)
        store = ScrollStore(name)
        try:
            selected = store.selection()
        except ValueError:
            # Re-import is also the recovery action for a corrupt old choice.
            store.select('')
            selected = ''
        if selected:
            record = CredentialCatalog().read().get(selected)
            if (not record or record.get('source_script') != name
                    or record.get('binding') != binding(settings)):
                # Imports are scoped to one configuration. Old versions could
                # retain a different source's choice even after a successful read.
                # Leave the new role list unselected instead of choosing a role
                # for the user or keeping an unrecoverable unavailable label.
                store.select('')
        response = info(name)
        response['message'] = (f'已读取 {len(records)} 个角色' if records else
                               '未找到可用凭证，请先在该模拟器登录查询小号，再重新读取')
        return response


def select_account(name, identifier):
    name, settings = configuration(name)
    with selection_lock(name).acquire(timeout=0):
        if identifier:
            record = CredentialCatalog().read().get(identifier)
            if not record or record.get('source_script') != name or not _source_valid(record):
                raise ScrollError('account_missing', '账号来源已失效，请重新读取本地账号', 409)
        # Results are tagged by selection, so failed queries cannot expose a prior role's values.
        ScrollStore(name).select(identifier)
        return info(name)


def _memory_result(snapshot, identity, activity):
    rows = progress_rows(snapshot, activity)
    for row in rows:
        rank = snapshot.get('_rankings', {}).get(row['painting_id'])
        if rank:
            row.update({key: rank[key] for key in ('rank_20_score', 'rank_100_score', 'rankings',
                        'rank_source', 'rank_queried_at') if key in rank})
    return {
        'identity': identity, 'source': 'game_memory', 'freshness': 'client_cache',
        'queried_at': now_string(), 'scrolls': rows,
        'inventory': fragment_counts(snapshot.get('_currency'), activity.fragment_ids),
        'activity': activity.metadata(),
        'warnings': ['显示游戏已接收的缓存，读取时间表示本次内存取样时间，服务器更新取决于游戏自身刷新。',
                     '本次内存读取未取得排行榜，前20和前100分数暂缺。'],
    }


def _selected_memory_result(device, record, activity, *, cached_only=False, timeout=None):
    if not device.supports_live_memory:
        raise ScrollError('game_running', '当前设备暂不支持运行中读取绘卷，请先关闭游戏，再独立查询', 409)
    expected = record['account']
    runtime = device.game_runtime()
    try:
        options = {'selected_only': True, 'fragment_ids': activity.fragment_ids}
        if cached_only:
            options['cached_only'] = True
        if timeout is not None:
            options['timeout'] = timeout
        actual, snapshot = read_game_memory(device.host_pid(), runtime, expected,
                                            **options)
    except LiveRoleMismatch as error:
        if device.game_runtime() != runtime:
            raise ScrollError('game_restarted', '查询期间游戏进程发生变化，请重试', 409) from error
        actual_name = getattr(error, 'actual_name', '')
        expected_name = record.get('identity', {}).get('name', '所选角色')
        message = (f'当前游戏中是“{actual_name}”，与所选“{expected_name}”不同' if actual_name else
                   '当前在线角色与所选角色不一致')
        failure = ScrollError('role_mismatch', message, 409)
        failure.actual_name = actual_name
        failure.actual_role_id = getattr(error, 'actual_role_id', '')
        raise failure from error
    except LiveCacheUnavailable as error:
        cause = error.__cause__ or error
        logger.warning(f'绘卷内存读取失败：{type(cause).__name__}: {cause}')
        raise ScrollError('cache_unavailable', '已确认所选角色，但未能完整读取游戏中的绘卷数据；未发起独立登录', 409) from error
    if device.game_runtime() != runtime:
        raise ScrollError('game_restarted', '查询期间游戏进程发生变化，请重试', 409)
    if actual is None:
        raise ScrollError('identity_unreadable', '游戏正在运行，但无法确认在线角色，已停止查询以避免顶号，请保持角色登录后重试', 409)
    if (actual.get('uid') != expected['uid'] or actual.get('user_id') != expected['user_id']
            or str(actual.get('serverid')) != expected['serverid']):
        raise ScrollError('role_mismatch', '所选角色与当前游戏中的角色不一致，已停止查询以避免顶号，请选择当前在线角色或关闭游戏后再查', 409)
    if snapshot is None:
        raise ScrollError('cache_unavailable', '所选角色在线，但未读到绘卷缓存，已停止查询；请在游戏内打开绘卷页面后重试', 409)
    identity = snapshot.get('_identity')
    if not identity:
        raise ScrollError('identity_unreadable', '未能校验当前游戏角色的绘卷对象，已停止查询以避免使用历史缓存，请重试', 409)
    if (identity.get('role_id') != expected['user_id']
            or str(identity.get('server_id')) != expected['serverid']):
        raise ScrollError('role_mismatch', '游戏缓存不属于所选角色，已停止查询，请重试', 409)
    return _memory_result(snapshot, identity, activity)


def _confirm_source_offline(device, record, activity):
    if not device.game_alive():
        return
    try:
        _selected_memory_result(device, record, activity, cached_only=True)
    except ScrollError as error:
        if error.code == 'role_mismatch':
            return
        raise ScrollError('game_started', '来源设备中的游戏已启动，尚未确认所选账号离线，请重新查询', 409) from error
    raise ScrollError('game_started', '所选账号已在线，请重新查询以读取游戏缓存', 409)


def _independent_result(name, device, record, activity):
    account = record['account']
    with query_stage(name, '网关地址查询'):
        address = gate_address(account['serverid'])
    _confirm_source_offline(device, record, activity)
    try:
        with query_stage(name, '凭证查询及绘卷排行'):
            return query_details(account, address, strict_role=True, activity=activity)
    except PermissionError as error:
        raise ScrollError('authorization_expired', '所选账号凭证已失效，请在来源模拟器登录该账号，再重新读取本地账号', 401) from error


def _query_selected(name, settings, store, identifier, activity):
    record = CredentialCatalog().read().get(identifier)
    if not record or record.get('source_script') != name or not _source_valid(record):
        raise ScrollError('account_missing', '所选账号来源已失效，请重新读取或选择账号', 409)
    account = record['account']
    with device_lock(record['serial']).acquire(timeout=0), account_lock(account).acquire(timeout=0):
        with query_stage(name, '来源设备检查'):
            device = ScrollDevice(settings, saved_instance=record)
            if device.serial != record['serial']:
                raise ScrollError('device_changed', '账号来源设备发生变化，请重新读取账号', 409)
            alive = device.game_alive()
        if alive:
            with query_stage(name, '读取游戏内存'):
                try:
                    result = _selected_memory_result(device, record, activity)
                except ScrollError as error:
                    if error.code != 'role_mismatch':
                        raise
                    logger.info(f'绘卷查询 [{name}] 当前配置运行的角色与所选角色不同，使用所选凭证查询')
                    result = _independent_result(name, device, record, activity)
        else:
            logger.info(f'绘卷查询 [{name}] 当前配置的游戏未运行，使用所选凭证查询')
            result = _independent_result(name, device, record, activity)
    identity = result['identity']
    if not activity.matches(result):
        raise ScrollError('activity_mismatch', '绘卷期次已变化，请重新查询')
    if identity.get('role_id') != account['user_id'] or str(identity.get('server_id')) != account['serverid']:
        raise ScrollError('role_mismatch', '服务器返回的角色或区服与所选账号不一致')
    identity['server_name'] = record['identity'].get('server_name', '')
    result = enrich_rankings(result, activity)
    if result.get('source') == 'game_memory':
        result = _supplement_rankings(name, record, result, activity)
    result['_binding'] = binding(settings)
    result['_credential'] = identifier
    store.save_result(result)
    return info(name, activity)


def _supplement_rankings(name, selected, result, activity):
    """Use an imported, same-server offline account without replacing inventory."""
    if all(row.get('rank_source') == 'game_cache'
           and row.get('rank_20_score') is not None and row.get('rank_100_score') is not None
           for row in result['scrolls']):
        return result
    catalog = CredentialCatalog().read()
    account = selected['account']
    seen = set()
    for helper in catalog.values():
        candidate = helper['account']
        key = (candidate.get('uid'), candidate.get('user_id'), str(candidate.get('serverid')))
        if (key in seen or candidate.get('uid') == account['uid']
                or str(candidate.get('serverid')) != str(account['serverid'])):
            continue
        seen.add(key)
        if not _source_valid(helper):
            continue
        source = helper['source_script']
        attempted = False
        try:
            _, settings = configuration(source)
            device = ScrollDevice(settings, saved_instance=helper)
            if device.serial != helper['serial']:
                continue
            with device_lock(device.serial).acquire(timeout=0), account_lock(candidate).acquire(timeout=0):
                # Apply the same current-source rule to a ranking helper.
                _confirm_source_offline(device, helper, activity)
                logger.info(f'绘卷查询 [{name}] 使用同区服离线角色“{helper["identity"].get("name", "")}”补全排行')
                attempted = True
                rankings = _independent_result(name, device, helper, activity)
            identity = rankings.get('identity', {})
            if (identity.get('role_id') != candidate.get('user_id')
                    or str(identity.get('server_id')) != str(account['serverid'])
                    or not activity.matches(rankings)):
                raise ValueError('补全排名的角色、区服或期次不匹配')
            enrich_rankings(rankings, activity)
            return enrich_rankings(result, activity)
        except (ScrollError, OSError, RuntimeError, ValueError, Timeout) as error:
            logger.warning(f'绘卷查询 [{name}] 同服排行补全暂不可用（{getattr(error, "code", type(error).__name__)}）')
            # A slow/expired credential must not trigger a chain of logins.
            if attempted:
                break
    result.setdefault('warnings', []).append('暂无可用的同服离线号刷新排行，已有排名保留原采集时间。')
    return result


def query(name):
    try:
        with selection_lock(name).acquire(timeout=0):
            return _query(name)
    except Timeout as error:
        raise ScrollError('query_busy', '该配置正在查询或切换账号，请稍后重试', 409) from error


def _query(name):
    name, settings = configuration(name)
    store = ScrollStore(name)
    try:
        with query_stage(name, '当前期次校验'):
            activity = current_activity()
        selected = store.selection()
        if selected:
            return _query_selected(name, settings, store, selected, activity)
        device = ScrollDevice(settings)
        with device_lock(device.serial).acquire(timeout=0):
            try:
                record = store.read()
            except (RuntimeError, ValueError, OSError):
                record = None
            if record and record.get('binding') != binding(settings):
                record = None
            previous_account = record.get('account', {}) if record else {}
            if device.game_alive():
                if not device.supports_live_memory:
                    raise ScrollError('game_running', 'redroid 暂不支持运行中读取绘卷，请先关闭该设备中的阴阳师，再独立查询', 409)
                runtime = device.game_runtime()
                account, snapshot = read_game_memory(device.host_pid(), runtime, previous_account,
                                                     fragment_ids=activity.fragment_ids)
                if device.game_runtime() != runtime:
                    raise ScrollError('game_restarted', '查询期间游戏进程发生变化，请重试')
                if account is None:
                    raise ScrollError('identity_unreadable', '游戏正在运行，但未能解析本次登录身份。请保持游戏页面后重试')
                same_account = (previous_account.get('uid') == account['uid']
                                and previous_account.get('user_id') == account.get('user_id')
                                and previous_account.get('serverid') == account.get('serverid'))
                identity = (record or {}).get('identity') if same_account else None
                record = {'binding': binding(settings), 'account': account, 'identity': identity,
                          'gateway': device.gate_address(), 'captured_at': now_string()}
                cached = store.result()
                cached_identity = cached.get('identity', {}) if cached else {}
                same_role = (same_account and cached and activity.matches(cached) and not cached.get('_credential')
                             and cached_identity.get('role_id') == account.get('user_id')
                             and cached_identity.get('server_id') == account.get('serverid'))
                if same_role:
                    record['identity'] = cached_identity
                    cached['_binding'] = binding(settings)
                    store.save_result(cached)
                elif not same_account:
                    store.clear_result()
                store.save(record)
                if snapshot is None:
                    response = info(name, activity)
                    response['message'] = ('身份已自动保存。本次内存读取未命中绘卷进度，下面保留上次查询结果及其原始时间。'
                                           if response['result'] else
                                           '身份已自动保存。本次内存读取未命中绘卷进度，游戏关闭后可独立查询服务器最新值。')
                    return response
                identity = snapshot.get('_identity') or record.get('identity')
                result = _memory_result(snapshot, identity or {
                    'name': '游戏中的当前角色', 'server_id': str(account.get('serverid', '')),
                    'role_id': str(account.get('user_id', ''))}, activity)
                record['identity'] = result['identity']
                store.save(record)
            else:
                if not record:
                    message = ('请先点击“读取账号”，选择查询角色后再查询绘卷' if not device.supports_live_memory else
                               '首次使用请先在该模拟器登录阴阳师，再点击查询以自动保存授权')
                    raise ScrollError('authorization_missing', message)
                if not record.get('gateway'):
                    raise ScrollError('gateway_missing', '尚未保存游戏网关，请登录一次游戏后点击查询')
                with account_lock(record['account']).acquire(timeout=0):
                    result = query_details(record['account'], record['gateway'], activity=activity)
                record['identity'] = result['identity']
                record['account']['user_id'] = result['identity']['role_id']
                store.save(record)
            result = enrich_rankings(result, activity)
            result['_binding'] = binding(settings)
            store.save_result(result)
            return info(name, activity)
    except ScrollError:
        raise
    except Timeout as error:
        raise ScrollError('query_busy', '该模拟器或账号正在查询，请稍后重试', 409) from error
    except PermissionError as error:
        raise ScrollError('authorization_expired', str(error), 401) from error
    except TimeoutError as error:
        raise ScrollError('query_timeout', '绘卷查询超时，请稍后重试', 504) from error
    except (ValueError, RuntimeError) as error:
        raise ScrollError('query_unavailable', str(error)) from error
    except OSError as error:
        raise ScrollError('connection_failed', '连接或读取失败，请检查设备状态和网络后重试', 503) from error
