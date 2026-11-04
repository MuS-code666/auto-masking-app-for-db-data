"""AI（端末内の LLM）の設定。端末ごとの設定のため、アプリ用フォルダに保存する（値を含まない）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from maskapp.infra.paths import app_home

DEVICES = ("GPU", "NPU", "CPU")


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool = False
    backend: str = "openvino"  # "openvino"（試験では "fake"）
    model_dir: str = ""  # OpenVINO 形式のモデルのフォルダ
    device: str = "GPU"
    timeout_s: float = 120.0  # 1回の問い合わせの上限
    batch_size: int = 10  # 1回に問い合わせる文章の数

    @property
    def usable(self) -> bool:
        return self.enabled and (self.backend == "fake" or bool(self.model_dir) and Path(self.model_dir).is_dir())

    @property
    def model_name(self) -> str:
        """結果の記録に使うモデルの識別（フォルダ名。パスは記録しない）。"""
        return Path(self.model_dir).name if self.model_dir else self.backend

    @staticmethod
    def path() -> Path:
        return app_home() / "llm.json"

    @classmethod
    def load(cls) -> LLMSettings:
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
