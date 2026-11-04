"""LLM ワーカーの起動・問い合わせ・停止（LLMProvider）。

時間切れ・異常終了・読込失敗は None（AI候補なし）として返し、ワーカーは次の問い合わせで起動し直す。
続けて失敗した回数が上限に達したら LLMUnavailable にし、それ以後は問い合わせない。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from enum import StrEnum
from pathlib import Path

from maskapp.infra.paths import app_home
from maskapp.infra.safelog import get_logger
from maskapp.llm.settings import LLMSettings
from maskapp.llm.worker import MARKER

_log = get_logger("llm")
LOAD_TIMEOUT_S = 900.0  # 初回は GPU 向けの変換に時間がかかる
MAX_FAILURES = 3


class Failure(StrEnum):
    TIMEOUT = "timeouts"
    CRASH = "crashes"
    ERROR = "errors"


class LLMUnavailable(RuntimeError):
    """AI を使えない（未設定、読込失敗、続けて失敗）。"""


class LLMClient:
    def __init__(self, settings: LLMSettings, env: dict[str, str] | None = None) -> None:
        self.settings = settings
        self._env = env
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        self.failures = 0  # 続けて失敗した回数
        self.stats = {"requests": 0, "timeouts": 0, "crashes": 0, "errors": 0}

    # ---------------------------------------------------------------- ワーカー

    def _command(self) -> list[str]:
        s = self.settings
        cmd = [sys.executable, "-m", "maskapp.llm.worker", "--backend", s.backend, "--device", s.device]
        if s.model_dir:
            cmd += ["--model", s.model_dir, "--cache-dir", str(app_home() / "model_cache")]
        return cmd

    def _start(self) -> None:
        if not self.settings.usable:
            raise LLMUnavailable("ai is not configured")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(self._env or {})}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", env=env, creationflags=flags, cwd=str(Path(__file__).resolve().parents[2]),
        )
        self._lines = queue.Queue()
        threading.Thread(target=self._read, args=(self._proc, self._lines), daemon=True).start()
        ready = self._receive(LOAD_TIMEOUT_S)
        if not ready or not ready.get("ready"):
            self.stop()
            self.failures = MAX_FAILURES
            _log.event("llm_load_failed")
            raise LLMUnavailable("the model could not be loaded")
        _log.event("llm_worker_started")

    @staticmethod
    def _read(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:
            if line.startswith(MARKER):
                lines.put(line[len(MARKER):])
        lines.put(None)  # 終了

    def _receive(self, timeout: float) -> dict | None:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            return {"exited": True}
        try:
            return json.loads(line)
        except ValueError:
            return {"error": "bad_line"}

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.kill()
            proc.wait()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------------------------------------------------------------- 問い合わせ

    @property
    def available(self) -> bool:
        return self.settings.usable and self.failures < MAX_FAILURES

    def generate(self, system: str, user: str, *, task: str = "", max_new_tokens: int = 512,
                 schema: dict | None = None, extra: dict | None = None) -> str | None:
        """生成した文字列。失敗（時間切れ・異常終了・エラー）なら None。"""
        if not self.available:
            raise LLMUnavailable("ai is not available")
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        self._next_id += 1
        request = {"id": self._next_id, "task": task, "system": system, "user": user,
                   "max_new_tokens": max_new_tokens, "schema": schema, **(extra or {})}
        self.stats["requests"] += 1
        try:
            self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except OSError:
            return self._failed(Failure.CRASH)
        while True:
            reply = self._receive(self.settings.timeout_s)
            if reply is None:
                self._kill()
                return self._failed(Failure.TIMEOUT)
            if reply.get("exited"):
                self._kill()
                return self._failed(Failure.CRASH)
            if reply.get("id") != self._next_id:
                continue  # 前の（時間切れにした）問い合わせへの応答
            if "text" not in reply:
                return self._failed(Failure.ERROR)
            self.failures = 0
            return reply["text"]

    def _failed(self, kind: Failure) -> None:
        self.stats[kind.value] += 1
        self.failures += 1
        _log.event("llm_request_failed", kind=kind, failures=self.failures)
        return None
