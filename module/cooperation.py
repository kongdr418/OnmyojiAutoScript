"""Local battle coordination. Never write another instance's config."""
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from module.exception import ScriptError, TaskEnd
from module.logger import logger
from module.config.utils import convert_to_underscore


BATTLE_TASKS = frozenset(('Orochi', 'EvoZone', 'Exploration', 'FallenSun',
                         'EternitySea', 'BondlingFairyland', 'OtherWorldTwilight', 'MetaDemon',
                         'MetaDemonTicket'))


def instance_name(value):
    value = str(value).strip()
    if value and (value in ('.', '..') or any(c in value for c in '\\/:*?"<>|\r\n,，;；')):
        raise ValueError('协同脚本只能填写一个本地实例配置名')
    return value


def ticket_peer_names(value):
    names = tuple(dict.fromkeys(instance_name(name) for name in re.split(r'[\r\n,，;；]+', str(value))
                                if name.strip()))
    if len(names) > 2:
        raise ValueError('刷票协同最多填写两名队员的本地实例配置名')
    return names


class Cooperation:
    def __init__(self, name, root=None):
        self.name = instance_name(name)
        self.root = Path(root or Path.cwd())
        folder = self.root / 'data' / 'cooperation'
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / 'state.sqlite3'
        self.active_task = ''
        self._stop = threading.Event()
        self._thread = None
        self.peers = {}
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runners
                  (name TEXT PRIMARY KEY, task TEXT, heartbeat REAL);
                CREATE TABLE IF NOT EXISTS wakes
                  (target TEXT, task TEXT, source TEXT, created REAL,
                   PRIMARY KEY(target, task));
                CREATE TABLE IF NOT EXISTS waiting
                  (name TEXT, task TEXT, peer TEXT, since REAL,
                   PRIMARY KEY(name, task));
                CREATE TABLE IF NOT EXISTS assignments
                  (name TEXT, task TEXT, peer TEXT, PRIMARY KEY(name, task));
                CREATE TABLE IF NOT EXISTS room_checks
                  (source TEXT, target TEXT, task TEXT, token TEXT,
                   created REAL, ready INTEGER, answered REAL, PRIMARY KEY(source, target, task));
                CREATE TABLE IF NOT EXISTS task_completions
                  (source TEXT, target TEXT, task TEXT, created REAL, reason TEXT,
                   PRIMARY KEY(source, target, task));
                CREATE TABLE IF NOT EXISTS battle_states
                  (source TEXT, target TEXT, task TEXT, session TEXT, payload TEXT, updated REAL,
                   PRIMARY KEY(source, target, task));
            ''')
            db.execute('BEGIN IMMEDIATE')
            primary_key = [row[1] for row in db.execute('PRAGMA table_info(room_checks)') if row[5]]
            if primary_key == ['source']:
                db.execute('ALTER TABLE room_checks RENAME TO room_checks_old')
                db.execute('CREATE TABLE room_checks '
                           '(source TEXT, target TEXT, task TEXT, token TEXT, created REAL, '
                           'ready INTEGER, answered REAL, PRIMARY KEY(source, target, task))')
                db.execute('INSERT INTO room_checks SELECT * FROM room_checks_old')
                db.execute('DROP TABLE room_checks_old')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def publish(self, task=None):
        changed = task is not None and task != self.active_task
        if task is not None:
            self.active_task = task
        with self.connect() as db:
            if changed:
                db.execute('DELETE FROM room_checks WHERE source=? OR target=?',
                           (self.name, self.name))
                if task == 'MetaDemonTicket':
                    db.execute('DELETE FROM battle_states WHERE source=? AND task=?', (self.name, task))
            db.execute('INSERT OR REPLACE INTO runners VALUES (?, ?, ?)',
                       (self.name, self.active_task, time.time()))

    def start(self):
        self.publish('')
        self._owner_thread = threading.current_thread()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True,
                                        name='battle-cooperation')
        self._thread.start()

    def _heartbeat(self):
        while not self._stop.wait(5):
            try:
                if not self._owner_thread.is_alive():
                    with self.connect() as db:
                        db.execute('DELETE FROM runners WHERE name=?', (self.name,))
                    return
                self.publish()
            except sqlite3.Error as exc:
                logger.warning(f'Cooperation heartbeat: {exc}')

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)
        with self.connect() as db:
            db.execute('DELETE FROM room_checks WHERE source=? OR target=?',
                           (self.name, self.name))
            db.execute('DELETE FROM runners WHERE name=?', (self.name,))

    def request_room_check(self, task, peer):
        token = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO room_checks VALUES (?, ?, ?, ?, ?, NULL, NULL)',
                       (self.name, peer, task, token, time.time()))
        return token

    def publish_battle_state(self, task, peer, session, payload):
        """使用同一协同数据库交换轮次内的 JSON 状态。"""
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO battle_states VALUES (?, ?, ?, ?, ?, ?)',
                       (self.name, peer, task, session, json.dumps(payload, ensure_ascii=False), time.time()))

    def peer_battle_state(self, task, peer):
        with self.connect() as db:
            row = db.execute('SELECT session, payload, updated FROM battle_states '
                             'WHERE source=? AND target=? AND task=?',
                             (peer, self.name, task)).fetchone()
        return {'session': row[0], 'payload': json.loads(row[1]), 'updated': row[2]} if row else None

    def begin_completion_phase(self, task, peer):
        """开始新一轮角色阶段时，作废本实例此前的完成通知。"""
        with self.connect() as db:
            db.execute('DELETE FROM task_completions WHERE source=? AND target=? AND task=?',
                       (self.name, peer, task))
        return time.time()

    def notify_task_completion(self, task, peer, reason):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO task_completions VALUES (?, ?, ?, ?, ?)',
                       (self.name, peer, task, time.time(), reason))
        logger.info(f'Cooperation completed: {self.name} -> {peer}/{task}: {reason}')

    def peer_task_completion(self, task, peer, since):
        with self.connect() as db:
            row = db.execute('SELECT created, reason FROM task_completions '
                             'WHERE source=? AND target=? AND task=?',
                             (peer, self.name, task)).fetchone()
        return row[1] if row and row[0] >= since else None

    def pending_room_check(self, task, peer):
        with self.connect() as db:
            row = db.execute('SELECT token, created FROM room_checks WHERE source=? '
                             'AND target=? AND task=? AND answered IS NULL',
                             (peer, self.name, task)).fetchone()
        return row[0] if row and 0 <= time.time() - row[1] <= 5 else None

    def answer_room_check(self, task, peer, token, ready):
        with self.connect() as db:
            db.execute('UPDATE room_checks SET ready=?, answered=? WHERE source=? '
                       'AND target=? AND task=? AND token=? AND answered IS NULL',
                       (bool(ready), time.time(), peer, self.name, task, token))

    def room_check_result(self, task, peer, token):
        now = time.time()
        with self.connect() as db:
            row = db.execute('SELECT ready, answered, created FROM room_checks '
                             'WHERE source=? AND target=? AND task=? AND token=?',
                             (self.name, peer, task, token)).fetchone()
            runner = db.execute('SELECT task, heartbeat FROM runners WHERE name=?',
                                (peer,)).fetchone()
        if not row or not runner or runner[0] != task or now - runner[1] >= 30:
            return False
        if now - row[2] > 5:
            return False
        if row[1] is None:
            return None
        return bool(row[0]) and 0 <= now - row[1] <= 3

    def read(self, name):
        name = instance_name(name)
        path = self.root / 'config' / f'{name}.json'
        if not path.is_file():
            raise ScriptError(f'协同脚本不存在：{name}')
        with path.open(encoding='utf-8-sig') as stream:
            data = json.load(stream)
        if 'meta_demon_ticket' not in data and 'meta_demon' in data:
            from tasks.MetaDemonTicket.config import split_legacy_meta_demon
            data = split_legacy_meta_demon(data)
        return data

    def ticket_group(self):
        """解析同一玩法的一名队长和一至两名队员，队长排在首位。"""
        entries = {path.stem: self.read(path.stem).get('meta_demon_ticket', {})
                   for path in (self.root / 'config').glob('*.json')}
        targets = {name: ticket_peer_names(item.get('invite_config', {}).get('cooperation_script', ''))
                   for name, item in entries.items() if item.get('scheduler', {}).get('enable')}
        own = entries.get(self.name, {})
        role = own.get('general_config', {}).get('user_status')
        own_targets = targets.get(self.name, ())
        incoming = [name for name, peers in targets.items() if self.name in peers]
        if not own_targets and not incoming:
            return ()
        if role == 'leader':
            leader = self.name
        elif role == 'member':
            candidates = set(own_targets or incoming)
            if len(candidates) != 1:
                raise ScriptError('刷票队员须关联一名队长')
            leader = candidates.pop()
        else:
            raise ScriptError('刷票协同须配置队长或队员身份')
        members = set(targets.get(leader, ()))
        members.update(name for name, peers in targets.items() if leader in peers)
        if leader in members or not 1 <= len(members) <= 2:
            raise ScriptError('刷票协同须配置一名队长和一至两名队员')
        group = (leader, *sorted(members))
        if self.name not in group:
            raise ScriptError('本实例不在刷票协同队伍中')
        mode = entries.get(leader, {}).get('general_config', {}).get('mode')
        if mode not in ('门票刷取：觉醒', '门票刷取：探索'):
            raise ScriptError('刷票协同玩法有误')
        if mode == '门票刷取：探索' and len(group) > 2:
            raise ScriptError('探索组队支持两人，三人刷票请选择觉醒')
        for name in group:
            item = entries.get(name, {})
            settings = item.get('general_config', {})
            expected_role = 'leader' if name == leader else 'member'
            if (name not in targets or settings.get('user_status') != expected_role
                    or settings.get('mode') != mode):
                raise ScriptError(f'刷票协同配置不匹配：{name}，请核对任务启用、身份和玩法')
            if name != leader and targets[name] not in ((), (leader,)):
                raise ScriptError(f'刷票队员只能关联队长：{name}')
        if any(name not in group and set(peers).intersection(group) for name, peers in targets.items()):
            raise ScriptError('刷票协同队伍存在其他实例的冲突关联')
        return group

    def ticket_peers(self):
        group = self.ticket_group()
        if not group:
            return ()
        return group[1:] if self.name == group[0] else (group[0],)

    def matches_meta_demon_tickets(self, peer):
        try:
            return peer in self.ticket_peers()
        except (ScriptError, ValueError):
            return False

    def validate(self, task, peer):
        peer = instance_name(peer)
        key = convert_to_underscore(task)
        if task not in BATTLE_TASKS or peer == self.name:
            raise ScriptError('组队身份有误')
        own, other = self.read(self.name).get(key, {}), self.read(peer).get(key, {})
        if task == 'MetaDemonTicket' and not self.matches_meta_demon_tickets(peer):
            raise ScriptError('超鬼王门票刷取协同配置不匹配')
        for name, item in ((self.name, own), (peer, other)):
            if not item.get('scheduler', {}).get('enable', False):
                raise ScriptError(f'协同任务未启用：{name}/{task}')
        if task == 'MetaDemon':
            return self._validate_meta_demon(own, other, peer)
        if task == 'MetaDemonTicket':
            return other
        def role(item):
            return next((v.get('user_status') for v in item.values()
                         if isinstance(v, dict) and 'user_status' in v), None)
        if {role(own), role(other)} != {'leader', 'member'}:
            raise ScriptError('组队身份有误')
        # A configured pair cannot also be the target of a third enabled instance.
        pair = {self.name, peer}
        for path in (self.root / 'config').glob('*.json'):
            item = self.read(path.stem).get(key, {})
            if not item.get('scheduler', {}).get('enable', False):
                continue
            target = item.get('invite_config', {}).get('cooperation_script', '').strip()
            if target and ((path.stem in pair and (target not in pair or target == path.stem))
                           or (path.stem not in pair and target in pair)):
                raise ScriptError('组队身份有误')
        for name, item in ((self.name, own), (peer, other)):
            logger.info(f'Cooperation {name}/{task}: next_run={item["scheduler"].get("next_run")}')
        return other

    def _validate_meta_demon(self, own, other, peer):
        """超鬼王协同：一端上供、一端协助，双方都开启鬼王协助模式。"""
        from tasks.MetaDemon.config import AssistanceRole, MetaDemonMode, OfferingMode
        pair = ((own, peer), (other, self.name))
        settings = [item.get('assistance_config', {}) for item, _ in pair]
        if any(setting.get('role') == AssistanceRole.OFFERING
               and setting.get('offering_mode', OfferingMode.COOPERATIVE) != OfferingMode.COOPERATIVE
               for setting in settings):
            raise ScriptError('上供端已选择不协同，无法匹配鬼王协助脚本')
        targets = [setting.get('cooperation_script', '').strip() for setting in settings]
        if ({setting.get('role') for setting in settings} != {AssistanceRole.OFFERING, AssistanceRole.ASSIST}
                or not all(MetaDemonMode.BOSS_ASSIST in item.get('meta_demon_config', {}).get('mode', [])
                           for item, _ in pair)
                or not any(targets)
                or any(target not in ('', name) for target, (_, name) in zip(targets, pair))):
            raise ScriptError('超鬼王协同双方须启用鬼王协助，并分别配置上供和协助身份')
        for item, name in pair:
            logger.info(f'Cooperation {name}/MetaDemon: next_run={item["scheduler"].get("next_run")}')
        return other

    def accepted_peer(self, task):
        with self.connect() as db:
            row = db.execute('SELECT peer FROM assignments WHERE name=? AND task=?',
                             (self.name, task)).fetchone()
        return row[0] if row else ''

    def finish(self, task):
        with self.connect() as db:
            waiting = db.execute('SELECT 1 FROM waiting WHERE name=? AND task=?',
                                 (self.name, task)).fetchone()
            if not waiting:
                db.execute('DELETE FROM assignments WHERE name=? AND task=?', (self.name, task))
                self.peers.pop(task, None)

    def requested(self):
        with self.connect() as db:
            return db.execute('SELECT task, source FROM wakes WHERE target=? ORDER BY created',
                              (self.name,)).fetchall()

    def runner_task(self, peer):
        with self.connect() as db:
            runner = db.execute('SELECT task, heartbeat FROM runners WHERE name=?', (peer,)).fetchone()
        return runner[0] if runner and 0 <= time.time() - runner[1] < 30 else None

    def wake_next(self, config, *, task_filter=None):
        """Only called at a scheduler boundary, so an active task is never interrupted."""
        for task, source in self.requested():
            if task_filter is not None and task != task_filter:
                continue
            key = convert_to_underscore(task)
            item = getattr(config.model, key, None)
            settings_group = 'assistance_config' if task == 'MetaDemon' else 'invite_config'
            configured = getattr(getattr(item, settings_group, None), 'cooperation_script', '')
            # Ignore obsolete requests when either side changed the pairing or disabled it.
            if not (self.root / 'config' / f'{source}.json').is_file():
                self.ack(task)
                continue
            if ((task == 'MetaDemon' and self.runner_task(source) != task)
                    or (task == 'MetaDemonTicket' and not self.matches_meta_demon_tickets(source))):
                self.ack(task)
                continue
            sender = self.read(source).get(key, {})
            sender_peer = sender.get(settings_group, {}).get('cooperation_script', '').strip()
            if (item is None or not item.scheduler.enable
                    or (task != 'MetaDemonTicket' and configured != source and sender_peer != self.name)):
                self.ack(task)
                continue
            if task == 'MetaDemon':
                try:
                    self.validate(task, source)
                except ScriptError as exc:
                    logger.info(f'忽略超鬼王协同唤醒：{exc}')
                    self.ack(task)
                    continue
            else:
                self.validate(task, source)
            config.reload()
            config.task_call(task, force_call=False)
            peers = getattr(config, '_cooperation_peers', None) or {}
            peers[task] = source
            config._cooperation_peers = peers
            self.peers[task] = source
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO assignments VALUES (?, ?, ?)',
                           (self.name, task, source))
            self.ack(task)
            logger.info(f'Cooperation wake: {self.name}/{task} requested by {source}')
            return task
        return None

    def ack(self, task):
        with self.connect() as db:
            db.execute('DELETE FROM wakes WHERE target=? AND task=?', (self.name, task))

    def skip_without_wake(self, task, peer, reason):
        """清除双方旧唤醒；仅向心跳有效的实例发布跳过通知，不创建唤醒。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM wakes WHERE task=? AND '
                       '((target=? AND source=?) OR (target=? AND source=?))',
                       (task, peer, self.name, self.name, peer))
            db.execute('DELETE FROM waiting WHERE task=? AND '
                       '((name=? AND peer=?) OR (name=? AND peer=?))',
                       (task, self.name, peer, peer, self.name))
            runner = db.execute('SELECT heartbeat FROM runners WHERE name=?', (peer,)).fetchone()
            if runner and 0 <= time.time() - runner[0] < 30:
                db.execute('INSERT OR REPLACE INTO task_completions VALUES (?, ?, ?, ?, ?)',
                           (self.name, peer, task, time.time(), reason))
                logger.info(f'Cooperation skip: {self.name} -> {peer}/{task}: {reason}')

    def prepare(self, config, task, peer, *, defer=True):
        if task == 'MetaDemonTicket':
            peers = self.ticket_peers()
            if peer not in peers:
                raise ScriptError('超鬼王门票刷取协同配置不匹配')
            pending = [name for name in peers if not self._prepare_peer(config, task, name, defer=False)]
            if pending:
                if defer:
                    self._defer_cooperation(config, task, pending[0])
                return False
            return True
        return self._prepare_peer(config, task, peer, defer=defer)

    def _prepare_peer(self, config, task, peer, *, defer=True):
        self.validate(task, peer)
        if task == 'MetaDemon':
            deadline = time.monotonic() + 30
            while self.runner_task(peer) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.info(f'超鬼王协同等待心跳超时：{peer}，已等待30秒')
                    break
                logger.info(f'超鬼王协同未检测到有效心跳：{peer}，5秒后重试，剩余{remaining:.0f}秒')
                time.sleep(min(5, remaining))
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM waiting WHERE name=? AND task=?', (self.name, task))
            runner = db.execute('SELECT task, heartbeat FROM runners WHERE name=?', (peer,)).fetchone()
            peer_waiting = db.execute('SELECT 1 FROM waiting WHERE name=? AND task=?',
                                      (peer, task)).fetchone()
            available = runner is not None and now - runner[1] < 30
            if task == 'MetaDemon' and (not available or runner[0] not in ('', task)):
                reason = runner[0] if available else '未运行'
                db.execute('DELETE FROM wakes WHERE target=? AND source=? AND task=?',
                           (peer, self.name, task))
                logger.info(f'超鬼王协同跳过：{peer}（{reason}）')
                return False
            if available and runner[0] == task and not peer_waiting:
                db.execute('DELETE FROM waiting WHERE task=? AND name IN (?, ?)',
                           (task, self.name, peer))
                db.execute('DELETE FROM wakes WHERE task=? AND target IN (?, ?)',
                           (task, self.name, peer))
                logger.info(f'Cooperation ready: {self.name} + {peer}/{task}')
                return True
            db.execute('INSERT OR REPLACE INTO wakes VALUES (?, ?, ?, ?)',
                       (peer, task, self.name, now))
            busy = not available or bool(runner[0])
            if task == 'MetaDemonTicket' and available and runner[0] == task:
                busy = False
            if busy:
                db.execute('INSERT OR REPLACE INTO waiting VALUES (?, ?, ?, ?)',
                           (self.name, task, peer, now))
        if busy:
            reason = runner[0] if available else '未运行'
            if not defer:
                logger.info(f'Cooperation waiting: {peer} ({reason}); {task} 本轮跳过')
                return False
            self._defer_cooperation(config, task, peer)
        logger.info(f'Cooperation requested: wake {peer}/{task}')
        return True

    def _defer_cooperation(self, config, task, peer):
        running = self.runner_task(peer)
        config.task_delay(task=task, success=False, server=False)
        target = getattr(config.model, convert_to_underscore(task)).scheduler.next_run
        config.task_runtime_outcome = {
            'task': task,
            'status': 'cooperation_waiting' if running is not None else 'cooperation_failed',
            'success': False,
            'reason': running if running is not None else '目标实例未运行',
            'peer': peer,
            'wait_until': target,
        }
        if running is None:
            logger.error(f'目标实例未运行：{peer}；{task} 同步失败，按任务失败间隔推迟至 {target}')
        logger.info(f'Cooperation waiting: {peer} ({running}); {task} retry at {target}, may be woken earlier')
        raise TaskEnd


def prepare_task_cooperation(config, task):
    """Check and wake the partner as soon as the scheduler activates this task."""
    item = getattr(config.model, convert_to_underscore(task))
    if task == 'MetaDemon' and not item.assistance_config.requires_cooperation:
        return
    settings_group = 'assistance_config' if task == 'MetaDemon' else 'invite_config'
    runtime = getattr(config, 'cooperation_runtime', None)
    if task == 'MetaDemonTicket':
        if runtime is None:
            runtime = Cooperation(config.config_name)
            config.cooperation_runtime = runtime
        peers = runtime.ticket_peers()
        if peers:
            runtime.peers[task] = peers[0]
            runtime.prepare(config, task, peers[0])
        return
    peer = getattr(getattr(item, settings_group, None), 'cooperation_script', '')
    if not peer:
        peer = (runtime.peers.get(task, '') if runtime is not None else '')
        peer = peer or (runtime.accepted_peer(task) if runtime is not None else '')
        peer = peer or (getattr(config, '_cooperation_peers', None) or {}).get(task, '')
    if peer:
        if runtime is None:
            runtime = Cooperation(config.config_name)
            config.cooperation_runtime = runtime
        runtime.peers[task] = peer
        runtime.prepare(config, task, peer)


def prepare_battle_cooperation(owner, task):
    """Fallback for direct task execution outside the scheduler."""
    if getattr(owner, '_battle_cooperation_prepared', False):
        return
    prepare_task_cooperation(owner.config, task)
    owner._battle_cooperation_task = task
    owner._battle_cooperation_prepared = True
