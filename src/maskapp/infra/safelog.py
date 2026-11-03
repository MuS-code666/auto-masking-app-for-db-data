"""元の値を記録しないログ。

方針（設計書 07 章 6.2）:
- ログに書けるのは「イベント名」と、数値・真偽・列挙型・UUID などの値を含まない項目だけ。
  文字列の項目は受け付けない（ファイル名・セル値・シート名を誤って渡すと TypeError）。
- 例外は型名と発生位置（ファイル名・行番号・関数名）だけを記録し、メッセージは記録しない。
  例外メッセージにはセルの値が入ることがあるため。
- 外部ライブラリや warnings からのログは、ロガー名と重要度だけ残し本文を捨てる。

使い方::

    log = get_logger("ingest")
    log.event("file_imported", file_id=3, rows=1000)
"""

from __future__ import annotations

import enum
import json
import logging
import logging.handlers
import re
import sys
import threading
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

LOGGER_PREFIX = "maskapp"
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SAFE_SCALARS = (bool, int, float, type(None))


class UnsafeLogValueError(TypeError):
    """値を含む恐れのある項目をログに渡したときの例外。"""


def _check_value(key: str, value: Any) -> Any:
    if isinstance(value, _SAFE_SCALARS):
        return value
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_check_value(key, v) for v in value]
    raise UnsafeLogValueError(
        f"field '{key}' has type {type(value).__name__}; only numbers, bools, enums and UUIDs are allowed"
    )


def safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in fields.items():
        if not _FIELD_NAME.match(key):
            raise UnsafeLogValueError(f"invalid field name: {key!r}")
        out[key] = _check_value(key, value)
    return out


class SafeLogger:
    """値を含まない項目だけを受け付けるロガー。"""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(f"{LOGGER_PREFIX}.{name}")

    def event(self, event: str, level: int = logging.INFO, **fields: Any) -> None:
        if not _EVENT_NAME.match(event):
            raise UnsafeLogValueError("event name must be a lowercase ascii identifier")
        checked = safe_fields(fields)
        self._logger.log(level, event, extra={"safe_fields": checked})

    def warning(self, event: str, **fields: Any) -> None:
        self.event(event, logging.WARNING, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.event(event, logging.ERROR, **fields)

    def exception(self, event: str, exc: BaseException, **fields: Any) -> None:
        """例外を記録する。例外メッセージは記録しない。"""
        if not _EVENT_NAME.match(event):
            raise UnsafeLogValueError("event name must be a lowercase ascii identifier")
        checked = safe_fields(fields)
        checked["error"] = describe_exception(type(exc), exc, exc.__traceback__)
        self._logger.log(logging.ERROR, event, extra={"safe_fields": checked})


def get_logger(name: str) -> SafeLogger:
    return SafeLogger(name)


def describe_exception(
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> dict[str, Any]:
    """例外を、型名と発生位置だけの辞書にする（メッセージ・変数の値は含めない）。"""
    frames = [
        {"file": Path(f.filename).name, "line": f.lineno, "func": f.name}
        for f in traceback.extract_tb(tb)
    ]
    info: dict[str, Any] = {
        "type": exc_type.__qualname__ if exc_type else None,
        "module": exc_type.__module__ if exc_type else None,
        "frames": frames,
    }
    cause = (exc.__cause__ or exc.__context__) if exc is not None else None
    if cause is not None and cause is not exc:
        info["cause"] = describe_exception(type(cause), cause, cause.__traceback__)
    return info


class SafeFormatter(logging.Formatter):
    """JSON 1行形式。本アプリのイベント以外は本文を捨てる。"""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
        }
        fields = getattr(record, "safe_fields", None)
        if fields is not None and record.name.startswith(LOGGER_PREFIX + "."):
            entry["event"] = record.msg
            entry["fields"] = fields
        else:
            entry["event"] = "external_log_suppressed"
        if record.exc_info:
            entry["error"] = describe_exception(*record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


_handler: logging.Handler | None = None


def configure_logging(directory: Path, level: int = logging.INFO) -> Path:
    """ログ出力先を設定し、未処理例外・warnings を値なしで記録するようにする。"""
    global _handler
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "maskapp.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    if _handler is not None:
        _handler.close()
    _handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _handler.setFormatter(SafeFormatter())
    root.addHandler(_handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    _install_exception_hooks()
    return path


def shutdown_logging() -> None:
    global _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler.close()
        _handler = None


_crash_log = logging.getLogger(f"{LOGGER_PREFIX}.crash")


def _log_crash(event: str, exc_type, exc, tb) -> None:
    _crash_log.error(
        event, extra={"safe_fields": {"error": describe_exception(exc_type, exc, tb)}}
    )


def _excepthook(exc_type, exc, tb) -> None:
    _log_crash("unhandled_exception", exc_type, exc, tb)
    # 標準エラーにも値を出さない（既定のフックはメッセージを表示するため置き換える）
    print(f"maskapp: unhandled {exc_type.__qualname__} (details in log)", file=sys.stderr)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    _log_crash("unhandled_thread_exception", args.exc_type, args.exc_value, args.exc_traceback)


def _unraisablehook(unraisable) -> None:
    _log_crash(
        "unraisable_exception",
        unraisable.exc_type,
        unraisable.exc_value,
        unraisable.exc_traceback,
    )


def _install_exception_hooks() -> None:
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisablehook
