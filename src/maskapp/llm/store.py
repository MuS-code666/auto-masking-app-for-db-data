"""AI で文章を検査した結果の保存（project.db の ai_text）。呼び出し側のトランザクション内で使う。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from maskapp.llm.tasks import AiCandidate


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_results(conn: sqlite3.Connection) -> dict[str, list[AiCandidate]]:
    """検査済みの文章（失敗を除く）→ 候補。複数のモデルで検査した文章は候補を合わせる。"""
    out: dict[str, list[AiCandidate]] = {}
    for digest, candidates in conn.execute("SELECT text_hash, candidates FROM ai_text WHERE status = 'ok' "
                                           "ORDER BY text_hash, model"):
        items = out.setdefault(digest, [])
        for c in json.loads(candidates):
            candidate = AiCandidate(c["text"], c["type"])
            if candidate not in items:
                items.append(candidate)
    return out


def checked_hashes(conn: sqlite3.Connection, model: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT text_hash FROM ai_text WHERE model = ? AND status = 'ok'", (model,))}


def save_result(conn: sqlite3.Connection, text: str, model: str, candidates: list[AiCandidate] | None) -> None:
    """candidates が None なら失敗（AI候補なし）として記録する。"""
    conn.execute(
        "INSERT OR REPLACE INTO ai_text (text_hash, model, status, candidates, created_at) VALUES (?, ?, ?, ?, ?)",
        (text_hash(text), model, "failed" if candidates is None else "ok",
         json.dumps([{"text": c.text, "type": c.type} for c in candidates or []], ensure_ascii=False),
         datetime.now(UTC).isoformat(timespec="milliseconds")),
    )
