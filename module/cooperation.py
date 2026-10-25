"""Local, two-instance battle coordination. Never write another instance's config."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from module.exception import ScriptError, TaskEnd
from module.logger import logger
from module.config.utils import convert_to_underscore


BATTLE_TASKS = frozenset(('Orochi', 'EvoZone', 'Exploration', 'FallenSun',
                         'EternitySea', 'BondlingFairyland', 'OtherWorldTwilight'))


def instance_name(value):
    value = str(value).strip()
    if value and (value in ('.', '..') or any(c in value for c in '\\/:*?"<>|\r\n,，;；')):
        raise ValueError('协同脚本只能填写一个本地实例配置名')
    return value


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
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def publish(self, task=None):
        if task is not None:
            self.active_task = task
        with self.connect() as db:
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
            db.execute('DELETE FROM runners WHERE name=?', (self.name,))

    def read(self, name):
        name = instance_name(name)
        path = self.root / 'config' / f'{name}.json'
        if not path.is_file():
            raise ScriptError(f'协同脚本不存在：{name}')
        with path.open(encoding='utf-8-sig') as stream:
            return json.load(stream)

    def validate(self, task, peer):
        peer = instance_name(peer)
        key = convert_to_underscore(task)
        if task not in BATTLE_TASKS or peer == self.name:
            raise ScriptError('组队身份有误')
        own, other = self.read(self.name).get(key, {}), self.read(peer).get(key, {})
        for name, item in ((self.name, own), (peer, other)):
            if not item.get('scheduler', {}).get('enable', False):
                raise ScriptError(f'协同任务未启用：{name}/{task}')
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

    def wake_next(self, config):
        """Only called at a scheduler boundary, so an active task is never interrupted."""
        for task, source in self.requested():
            key = convert_to_underscore(task)
            item = getattr(config.model, key, None)
            configured = getattr(getattr(item, 'invite_config', None), 'cooperation_script', '')
            # Ignore obsolete requests when either side changed the pairing or disabled it.
            if not (self.root / 'config' / f'{source}.json').is_file():
                self.ack(task)
                continue
            sender = self.read(source).get(key, {})
            sender_peer = sender.get('invite_config', {}).get('cooperation_script', '').strip()
            if (item is None or not item.scheduler.enable
                    or (configured != source and sender_peer != self.name)):
                self.ack(task)
                continue
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

    def prepare(self, config, task, peer):
        self.validate(task, peer)
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM waiting WHERE name=? AND task=?', (self.name, task))
            runner = db.execute('SELECT task, heartbeat FROM runners WHERE name=?', (peer,)).fetchone()
            peer_waiting = db.execute('SELECT 1 FROM waiting WHERE name=? AND task=?',
                                      (peer, task)).fetchone()
            available = runner is not None and now - runner[1] < 30
            if available and runner[0] == task and not peer_waiting:
                db.execute('DELETE FROM waiting WHERE task=? AND name IN (?, ?)',
                           (task, self.name, peer))
                db.execute('DELETE FROM wakes WHERE task=? AND target IN (?, ?)',
                           (task, self.name, peer))
                logger.info(f'Cooperation ready: {self.name} + {peer}/{task}')
                return
            db.execute('INSERT OR REPLACE INTO wakes VALUES (?, ?, ?, ?)',
                       (peer, task, self.name, now))
            busy = not available or bool(runner[0])
            if busy:
                db.execute('INSERT OR REPLACE INTO waiting VALUES (?, ?, ?, ?)',
                           (self.name, task, peer, now))
        if busy:
            reason = runner[0] if available else '未运行'
            config.task_delay(task=task, success=False, server=False)
            target = getattr(config.model, convert_to_underscore(task)).scheduler.next_run
            config.task_runtime_outcome = {
                'task': task,
                'status': 'cooperation_waiting' if available else 'cooperation_failed',
                'success': False,
                'reason': reason if available else '目标实例未运行',
                'peer': peer,
                'wait_until': target,
            }
            if not available:
                logger.error(f'目标实例未运行：{peer}；{task} 同步失败，按任务失败间隔推迟至 {target}')
            logger.info(f'Cooperation waiting: {peer} ({reason}); {task} retry at {target}, may be woken earlier')
            raise TaskEnd
        logger.info(f'Cooperation requested: wake {peer}/{task}')


def prepare_task_cooperation(config, task):
    """Check and wake the partner as soon as the scheduler activates this task."""
    item = getattr(config.model, convert_to_underscore(task))
    runtime = getattr(config, 'cooperation_runtime', None)
    peer = item.invite_config.cooperation_script
    if not peer:
        peer = (runtime.peers.get(task, '') if runtime is not None else '')
        peer = peer or (runtime.accepted_peer(task) if runtime is not None else '')
        peer = peer or (getattr(config, '_cooperation_peers', None) or {}).get(task, '')
    if peer:
        if runtime is None:
            runtime = Cooperation(config.config_name)
        runtime.prepare(config, task, peer)


def prepare_battle_cooperation(owner, task):
    """Fallback for direct task execution outside the scheduler."""
    if getattr(owner, '_battle_cooperation_prepared', False):
        return
    prepare_task_cooperation(owner.config, task)
    owner._battle_cooperation_prepared = True
