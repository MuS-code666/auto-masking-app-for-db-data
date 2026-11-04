"""AI（端末内の LLM）による補助：文章の候補の検出と、列の種類の推定（設計書 05 章 4.3〜4.4）。

AI は計画の作成とは別に動かす（時間がかかるため）。文章の検査結果は project.db に保存し、
計画の作成時に要確認の候補として使う。同じ文章は次の作業でも再利用し、問い合わせない。

画面では AI への問い合わせだけを別スレッドで行い、保存は画面のスレッド（project.db の接続を持つ側）で行う。
そのため、準備（prepare_scan）・問い合わせ（query_batches）・保存（save_batch）を分けている。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from maskapp.app.workbench import Workbench
from maskapp.config import ColumnSpec
from maskapp.config.suggest import spec_from_type
from maskapp.llm.client import LLMClient, LLMUnavailable
from maskapp.llm.store import checked_hashes, save_result, text_hash
from maskapp.llm.tasks import MASK_TOKEN, AiCandidate, find_candidates, guess_columns
from maskapp.transform.freetext import Dictionary, detect_candidates


@dataclass(frozen=True)
class ScanItem:
    text: str  # 元の文章（保存のハッシュに使う）
    sent: str  # AI に渡す文章（辞書の語を伏せたもの）


@dataclass
class ScanSummary:
    total: int = 0  # 作業の文章（異なる値）の数
    already: int = 0  # 前に検査済み
    checked: int = 0
    failed: int = 0
    stopped: bool = False  # 利用者が中止した、または AI が使えなくなった


def mask_known(dictionary: Dictionary, text: str) -> str:
    """辞書の語（置換する語）と規則で検出する候補（電話番号等）を伏せる。

    AI に渡す元の語句を減らし（設計書 05 章 4.3）、既に分かっている語を AI が答えて時間がかかるのを防ぐ。
    """
    matches = dictionary.find(text)
    covered = [False] * len(text)
    for m in matches:
        for i in range(m.start, m.end):
            covered[i] = True
    spans = sorted([(m.start, m.end) for m in matches]
                   + [(c.start, c.end) for c in detect_candidates(text, covered)])
    out, pos = [], 0
    for start, end in spans:
        if start < pos:
            continue
        out.append(text[pos:start])
        out.append(MASK_TOKEN)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def prepare_scan(wb: Workbench, run_id: int, model: str) -> tuple[list[ScanItem], ScanSummary]:
    texts = wb.freetext_texts(run_id)
    done = checked_hashes(wb.conn, model)
    dictionary = Dictionary.load(wb.conn)
    todo = [ScanItem(t, mask_known(dictionary, t)) for t in texts if text_hash(t) not in done]
    return todo, ScanSummary(total=len(texts), already=len(texts) - len(todo))


def query_batches(client: LLMClient, items: list[ScanItem], should_stop: Callable[[], bool] = lambda: False
                  ) -> Iterator[tuple[list[ScanItem], list[list[AiCandidate]] | None]]:
    """batch_size 件ずつ問い合わせる（画面では別スレッドで動かす）。AI が使えなくなったら止める。"""
    size = max(1, client.settings.batch_size)
    for start in range(0, len(items), size):
        if should_stop():
            return
        batch = items[start:start + size]
        try:
            result = find_candidates(client, [i.sent for i in batch])
        except LLMUnavailable:
            return
        yield batch, result


def save_batch(wb: Workbench, model: str, batch: list[ScanItem], result: list[list[AiCandidate]] | None,
               summary: ScanSummary) -> None:
    with wb.project.transaction() as conn:
        for i, item in enumerate(batch):
            candidates = None if result is None else [c for c in result[i] if c.text in item.text]
            save_result(conn, item.text, model, candidates)
    if result is None:
        summary.failed += len(batch)
    else:
        summary.checked += len(batch)


def scan_freetext(wb: Workbench, run_id: int, client: LLMClient,
                  should_stop: Callable[[], bool] = lambda: False) -> ScanSummary:
    """作業の文章を AI で検査して保存し、計画を作り直す（画面なしで使う版）。"""
    model = client.settings.model_name
    items, summary = prepare_scan(wb, run_id, model)
    done = 0
    for batch, result in query_batches(client, items, should_stop):
        save_batch(wb, model, batch, result, summary)
        done += len(batch)
    summary.stopped = done < len(items)
    wb.replan(run_id)
    return summary


def suggest_columns_by_ai(client: LLMClient, columns: list[tuple[str, list[str]]]) -> dict[str, ColumnSpec] | None:
    """列名と値の例から列の設定の候補を作る。AI が答えられなければ None。"""
    try:
        types = guess_columns(client, columns)
    except LLMUnavailable:
        return None
    if types is None:
        return None
    return {columns[i][0]: spec_from_type(columns[i][0], t) for i, t in types.items()}
