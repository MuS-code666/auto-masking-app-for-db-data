"""プロジェクトの同時使用を防ぐロック。

OS のファイルロックを使うため、アプリが異常終了してもロックは OS により解放され、
古いロックファイルが残って開けなくなることはない。
ただし Windows では、終了したプロセスのロックの解放が即時とは限らない（解放の時期は
システムの状況による）ため、取得は短時間再試行する。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class ProjectLockedError(RuntimeError):
    """プロジェクトが他で使用中。"""


class ProjectLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self, timeout: float = 0.0) -> None:
        """ロックを取得する。timeout 秒まで再試行し、取れなければ ProjectLockedError。"""
        if self._fh is not None:
            return
        deadline = time.monotonic() + timeout
        while True:
            fh = open(self._path, "a+b")  # noqa: SIM115 - 保持し続けるハンドル
            try:
                fh.seek(0)
                if os.name == "nt":
                    # LK_NBLCK は失敗時に即座に OSError（LK_LOCK は 1 秒ごとに 10 回再試行する）
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                fh.close()
                if time.monotonic() >= deadline:
                    raise ProjectLockedError("project is open in another window or process") from exc
                time.sleep(0.1)
                continue
            self._fh = fh
            return

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
