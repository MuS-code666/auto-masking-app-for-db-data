"""AI（端末内の LLM）の計測：合成の文章で、未登録の名称の検出率・誤検出・時間を測る（設計書 08 章）。

  .venv/Scripts/python -m tests.llm.bench <モデルのフォルダ> [GPU|NPU|CPU] [--batch 10]

文章はすべて架空の名称から作る（正解つき）。辞書で置換済みの語は「〇〇」で伏せた形にする（本番と同じ）。
精度は合成データでの値であり、実データでの精度を示すものではない。
"""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass

from maskapp.llm import LLMClient, LLMSettings
from maskapp.llm.tasks import find_candidates
from tests.synthetic.generator import _STEMS, _SURNAMES

_COMPANY_SUFFIX = ["運輸", "食堂", "建設", "印刷", "企画", "物流", "製菓", "薬局"]
_PERSON_TEMPLATES = ["{}さんに確認", "{}より電話あり、折り返し要", "{}課長へ報告済み", "見積は{}さんが担当",
                     "{}氏の承認待ち"]
_COMPANY_TEMPLATES = ["{}に配送依頼", "{}から見積受領", "{}向けサンプル出荷", "{}へ請求書送付済み",
                      "納品先を{}に変更"]
_GENERIC = ["特記事項なし", "前回同様", "返品処理済み", "梱包注意\n天地無用", '"至急"対応、送料込',
            "単価改定 2026/4/1適用", "サンプル出荷, 請求なし", "〇〇様より追加注文", "〇〇の在庫確認",
            "納期調整（担当:〇〇）", "〇〇へ折り返し連絡", "入荷待ち、来週再確認", "検品済み・不良なし",
            "月末締め翌月払い", "数量訂正あり"]


@dataclass(frozen=True)
class Case:
    text: str
    truth: str | None
    kind: str  # person / company / generic


def cases(seed: int = 7, n: int = 60) -> list[Case]:
    r = random.Random(seed)
    out: list[Case] = []
    for i in range(n):
        if i % 3 == 0:
            name = r.choice(_SURNAMES)
            out.append(Case(r.choice(_PERSON_TEMPLATES).format(name), name, "person"))
        elif i % 3 == 1:
            name = r.choice(_STEMS) + r.choice(_COMPANY_SUFFIX)
            out.append(Case(r.choice(_COMPANY_TEMPLATES).format(name), name, "company"))
        else:
            out.append(Case(r.choice(_GENERIC), None, "generic"))
    return out


def run(model: str, device: str, batch: int = 10, n: int = 60) -> dict:
    settings = LLMSettings(enabled=True, model_dir=model, device=device, timeout_s=300, batch_size=batch)
    items = cases(n=n)
    found = wrong = failed = 0
    with LLMClient(settings) as client:
        t0 = time.time()
        client.generate("出力は OK だけ。", "OK", max_new_tokens=4)
        load = time.time() - t0
        t1 = time.time()
        for start in range(0, len(items), batch):
            chunk = items[start:start + batch]
            result = find_candidates(client, [c.text for c in chunk])
            if result is None:
                failed += len(chunk)
                continue
            for case, cands in zip(chunk, result, strict=True):
                texts = [c.text for c in cands]
                if case.truth and case.truth in texts:
                    found += 1
                wrong += sum(1 for t in texts if t != case.truth)
        elapsed = time.time() - t1
    truths = sum(1 for c in items if c.truth)
    return {"model": settings.model_name, "device": device, "batch": batch, "texts": len(items),
            "load_s": round(load, 1), "per_text_s": round(elapsed / len(items), 2),
            "recall": round(found / truths, 3), "false_candidates": wrong, "failed_texts": failed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("device", nargs="?", default="GPU")
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("-n", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(run(args.model, args.device, args.batch, args.n), ensure_ascii=False))


if __name__ == "__main__":
    main()
