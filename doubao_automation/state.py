"""线程安全的 JSON 状态存储：任务队列、账号状态都靠它持久化，可随时断点续跑。"""
import json
import os
import threading
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path


class JsonStore:
    _locks = {}
    _guard = threading.Lock()
    _replace_retry_delays = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.5)

    def __init__(self, path, initial: dict = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initial = deepcopy(initial or {})
        with self._guard:
            self._lock = self._locks.setdefault(str(self.path.resolve()), threading.RLock())

    @contextmanager
    def _file_lock(self):
        with self._lock, open(self.path.with_suffix('.lock'), 'a+b') as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b'0')
                handle.flush()
            deadline = time.monotonic() + 30
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'等待状态文件锁超时: {self.path}')
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return deepcopy(self.initial)
        with self.path.open(encoding='utf-8') as handle:
            return json.load(handle)

    def _replace_atomic(self, source):
        # External Windows readers can deny delete-sharing despite our lock.
        # Keep the same snapshot and both locks; retry delays total 1.13 seconds.
        for attempt in range(len(self._replace_retry_delays) + 1):
            try:
                os.replace(source, self.path)
                return
            except OSError as error:
                if (getattr(error, 'winerror', None) not in (5, 32)
                        or attempt == len(self._replace_retry_delays)):
                    raise
                time.sleep(self._replace_retry_delays[attempt])

    @contextmanager
    def txn(self):
        """with store.txn() as data: 修改 data，退出时自动落盘。"""
        with self._file_lock():
            data = self._read()
            yield data
            tmp = None
            transaction_error = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                                 prefix=self.path.name, suffix='.tmp', delete=False) as handle:
                    tmp = handle.name
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                self._replace_atomic(tmp)
            except BaseException as error:
                transaction_error = error
                raise
            finally:
                try:
                    if tmp and os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError as cleanup_error:
                    if transaction_error is None:
                        raise
                    transaction_error.add_note(f'临时状态文件清理失败：{cleanup_error}')

    def snapshot(self) -> dict:
        with self._file_lock():
            return self._read()

    def reset(self, initial: dict = None) -> None:
        with self.txn() as data:
            data.clear()
            data.update(deepcopy(initial or {}))
