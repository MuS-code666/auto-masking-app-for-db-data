"""ログに元の値が残らないことの試験（段階0 受入条件）。"""

from __future__ import annotations

import csv
import io
import json
import logging
import sys
import threading
import warnings
from pathlib import Path

import pytest

from maskapp.infra import safelog
from maskapp.infra.paths import log_dir
from maskapp.infra.safelog import UnsafeLogValueError, configure_logging, get_logger
from maskapp.project import Project


@pytest.fixture
def log_file():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    path = configure_logging(log_dir())
    yield path
    safelog.shutdown_logging()
    logging.captureWarnings(False)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    sys.excepthook, threading.excepthook, sys.unraisablehook = saved_hooks


def _entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_string_fields_are_rejected():
    log = get_logger("test")
    with pytest.raises(UnsafeLogValueError):
        log.event("cell_read", value="株式会社あおば商事")
    with pytest.raises(UnsafeLogValueError):
        log.event("cell_read", path=Path("売上明細.csv"))
    with pytest.raises(UnsafeLogValueError):
        log.event("株式会社あおば商事")
    with pytest.raises(UnsafeLogValueError):
        log.event("ok_event", **{"Bad-Name": 1})


def test_allowed_fields_are_written(log_file: Path):
    import enum
    import uuid

    class Fmt(enum.Enum):
        CSV = 1

    get_logger("test").event("file_imported", rows=1000, ok=True, fmt=Fmt.CSV, run=uuid.UUID(int=1), cols=[1, 2])
    entry = _entries(log_file)[-1]
    assert entry["event"] == "file_imported"
    assert entry["fields"] == {
        "rows": 1000, "ok": True, "fmt": "CSV", "run": str(uuid.UUID(int=1)), "cols": [1, 2],
    }


def test_exception_message_is_not_logged(log_file: Path):
    secret = "株式会社あおば商事"
    try:
        try:
            raise KeyError(secret)
        except KeyError as inner:
            raise ValueError(f"cannot map {secret}") from inner
    except ValueError as exc:
        get_logger("test").exception("mapping_failed", exc, row=3)
    text = log_file.read_text(encoding="utf-8")
    assert secret not in text
    entry = _entries(log_file)[-1]
    assert entry["fields"]["error"]["type"] == "ValueError"
    assert entry["fields"]["error"]["cause"]["type"] == "KeyError"
    assert entry["fields"]["error"]["frames"]


def test_full_flow_never_writes_synthetic_values(
    log_file: Path, tmp_path: Path, dataset, dataset_dir: Path, capsys: pytest.CaptureFixture
):
    """合成データを扱う一連の操作で、ログに機密値・ファイル名・シート名が出ないこと。"""
    log = get_logger("test")
    secret = dataset.truth["sensitive_values"][0]

    with Project.create(tmp_path, "ログ試験") as project:
        for src in sorted(dataset_dir.iterdir()):
            dst = project.root / "originals" / src.name
            dst.write_bytes(src.read_bytes())
            if src.suffix == ".csv":
                raw = src.read_bytes()
                encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "cp932"
                rows = list(csv.reader(io.StringIO(raw.decode(encoding), newline="")))
                log.event("csv_parsed", rows=len(rows), cols=len(rows[0]))
                # 誤って値を渡しても書き込まれない
                with pytest.raises(UnsafeLogValueError):
                    log.event("csv_cell", value=rows[1][4])
        project.backup()

    # 外部ライブラリのログ・warnings・未処理例外に値が含まれても本文は捨てられる
    logging.getLogger("thirdparty.lib").warning("could not parse %s", secret)
    logging.getLogger("maskapp.raw").error("raw message %s", secret)  # SafeLogger を通さない記録
    warnings.warn(f"odd value {secret}", UserWarning, stacklevel=1)
    try:
        raise RuntimeError(f"unexpected {secret}")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    thread = threading.Thread(target=lambda: (_ for _ in ()).throw(ValueError(secret)))
    thread.start()
    thread.join()

    text = log_file.read_text(encoding="utf-8")
    entries = _entries(log_file)
    assert any(e["event"] == "csv_parsed" for e in entries)
    assert any(e["event"] == "external_log_suppressed" for e in entries)
    assert any(e["event"] == "unhandled_exception" for e in entries)
    assert any(e["event"] == "unhandled_thread_exception" for e in entries)

    forbidden = set(dataset.truth["sensitive_values"])
    forbidden.update(p.name for p in dataset_dir.iterdir())  # ファイル名
    forbidden.update(p.stem for p in dataset_dir.iterdir())
    forbidden.update(t.sheet for t in dataset.masters.values())  # シート名
    forbidden.add("ログ試験")  # プロジェクト名
    leaked = sorted(v for v in forbidden if v and v in text)
    assert leaked == []

    # 標準エラーにも値を出さない
    assert secret not in capsys.readouterr().err
