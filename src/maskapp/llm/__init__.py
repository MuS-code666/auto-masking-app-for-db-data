"""端末内で動かす LLM（設計書 02 章 1.3、05 章 4.3〜4.4）。

- LLM は別プロセス（ワーカー）で動かし、標準入出力のパイプでやり取りする（ローカルの HTTP ポートを開かない）。
  ワーカーも起動直後に外部通信を遮断する。
- ワーカーの異常終了・時間切れ・不正な出力は「AI候補なし」として扱い、本体の処理は続ける。
- LLM の出力は候補としてだけ使う（人が確認する）。置換処理に直接使わない。
"""

from maskapp.llm.client import LLMClient, LLMUnavailable
from maskapp.llm.settings import LLMSettings

__all__ = ["LLMClient", "LLMSettings", "LLMUnavailable"]
