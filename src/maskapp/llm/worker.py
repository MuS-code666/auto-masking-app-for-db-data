"""LLM のワーカープロセス：python -m maskapp.llm.worker --backend openvino --model <フォルダ> --device GPU

本体とは標準入出力で1行1件の JSON をやり取りする。応答の行は MARKER で始める（ライブラリが標準出力に
書く文字と区別するため）。起動直後に外部通信を遮断し、ログ・一時ファイルには何も書かない。

  起動完了  {"ready": true} / 読込失敗 {"ready": false, "error": "<例外の型名>"}
  要求      {"id": 1, "task": "...", "system": "...", "user": "...", "max_new_tokens": 512, "schema": {...}}
  応答      {"id": 1, "text": "..."} / 失敗 {"id": 1, "error": "<例外の型名>"}

試験用の backend "fake" は、環境変数 MASKAPP_FAKE_LLM で振る舞いを変える
（ok / hang / crash / garbage / slow:<秒>、語句の指定は MASKAPP_FAKE_TERMS="語1,語2"）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

MARKER = "@@maskapp "
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class OpenVinoBackend:
    def __init__(self, model: str, device: str, cache_dir: str | None) -> None:
        import openvino_genai as og

        self._og = og
        properties = {"CACHE_DIR": cache_dir} if cache_dir and device != "CPU" else {}
        self.pipe = og.LLMPipeline(model, device, **properties)

    def generate(self, request: dict) -> str:
        og = self._og
        config = og.GenerationConfig()
        config.max_new_tokens = int(request.get("max_new_tokens", 512))
        config.do_sample = False
        schema = request.get("schema")
        if schema and hasattr(og, "StructuredOutputConfig"):
            config.structured_output_config = og.StructuredOutputConfig(json_schema=json.dumps(schema))
        history = og.ChatHistory()
        history.append({"role": "system", "content": request.get("system", "")})
        history.append({"role": "user", "content": request.get("user", "")})
        tokenizer = self.pipe.get_tokenizer()
        # Qwen3 等の「考える」出力は使わない（時間がかかり、JSON 以外の文字が混ざる）
        prompt = tokenizer.apply_chat_template(history, add_generation_prompt=True,
                                               extra_context={"enable_thinking": False})
        result = self.pipe.generate(prompt, config, apply_chat_template=False)
        text = result if isinstance(result, str) else str(result)
        return _THINK.sub("", text).strip()


class FakeBackend:
    """試験用。入力の中にある指定の語句を候補として返す（文章の候補）、列の推定は固定の答え。"""

    def __init__(self) -> None:
        self.mode = os.environ.get("MASKAPP_FAKE_LLM", "ok")
        self.terms = [t for t in os.environ.get("MASKAPP_FAKE_TERMS", "").split(",") if t]

    def generate(self, request: dict) -> str:
        if self.mode == "hang":
            time.sleep(3600)
        if self.mode == "crash":
            os._exit(3)
        if self.mode.startswith("slow:"):
            time.sleep(float(self.mode.split(":", 1)[1]))
        if self.mode == "garbage":
            return "了解しました。候補は次のとおりです：さくら"
        if self.mode == "probe_network":
            import socket

            try:
                socket.create_connection(("192.0.2.1", 80), timeout=1).close()
            except OSError as exc:
                return type(exc).__name__
            except RuntimeError as exc:  # NetworkBlockedError
                return type(exc).__name__
            return "connected"
        if request.get("task") == "columns":
            return json.dumps({"columns": [{"i": i, "type": "顧客名", "action": "replace"}
                                           for i in range(request.get("count", 0))]}, ensure_ascii=False)
        items = []
        for line in request.get("user", "").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and "text" in item:
                for term in self.terms:
                    if term in item["text"]:
                        items.append([item["i"], term, "会社"])
                items.append([item["i"], "入力にない語", "他"])  # 検証で捨てられる
        return json.dumps({"c": items}, ensure_ascii=False)


def _write(obj: dict) -> None:
    sys.stdout.write(MARKER + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    from maskapp.infra.netguard import install_network_guard

    install_network_guard()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="openvino")
    parser.add_argument("--model", default="")
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--cache-dir", default=None)
    args = parser.parse_args(argv)
    try:
        backend = FakeBackend() if args.backend == "fake" else OpenVinoBackend(args.model, args.device,
                                                                                 args.cache_dir)
    except Exception as exc:  # noqa: BLE001 - 読込の失敗は型名だけを本体に返す
        _write({"ready": False, "error": type(exc).__name__})
        return
    _write({"ready": True})
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        try:
            _write({"id": request["id"], "text": backend.generate(request)})
        except Exception as exc:  # noqa: BLE001 - 失敗は型名だけを返す（入力の値を含めない）
            _write({"id": request["id"], "error": type(exc).__name__})


if __name__ == "__main__":
    main()
