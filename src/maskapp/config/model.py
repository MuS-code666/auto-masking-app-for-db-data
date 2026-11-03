"""変換設定の型（設計書 03 章 3）。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum


class ConfigError(ValueError):
    """設定の内容が不正。"""


class Action(StrEnum):
    KEEP = "keep"  # 維持
    REPLACE = "replace"  # 置換
    CLEAR = "clear"  # 内容削除
    FREETEXT = "freetext"  # 文章内の検出・置換（段階4で実装）


class Role(StrEnum):
    CODE = "code"  # 対象のコード
    NAME = "name"  # 対象の名称
    VALUE = "value"  # 対象を持たない値（住所・電話・メール等）


@dataclass(frozen=True)
class ColumnSpec:
    header: str
    action: Action = Action.KEEP
    info_type: str | None = None  # 画面表示用の情報種類（例 "顧客名"）。VALUE では置換の種類
    entity_type: str | None = None  # 置換対象の種類（CUSTOMER 等）
    role: Role | None = None
    code_scheme: str | None = None  # CODE のときのコード体系名
    binding: str | None = None  # 行内で組になる列の識別。既定は entity_type

    @property
    def binding_key(self) -> str | None:
        return self.binding or self.entity_type

    def validate(self) -> None:
        if self.action is Action.REPLACE:
            if self.role in (Role.CODE, Role.NAME):
                if not self.entity_type:
                    raise ConfigError(f"column '{self.header}': entity type is required")
                if self.role is Role.CODE and not self.code_scheme:
                    raise ConfigError(f"column '{self.header}': code scheme is required for code columns")
            elif self.role is Role.VALUE:
                if not self.info_type:
                    raise ConfigError(f"column '{self.header}': info type is required for value columns")
            else:
                raise ConfigError(f"column '{self.header}': role is required for replace")


@dataclass(frozen=True)
class TemplateSpec:
    """表の定義。見出しの並び（columns の順）で取込ファイルと照合する。"""

    name: str
    columns: list[ColumnSpec]
    header_row: int = 0  # 見出し行（0 起点）。それより上は表外（タイトル・注記）

    @property
    def headers(self) -> list[str]:
        return [c.header for c in self.columns]

    def column(self, header: str) -> ColumnSpec:
        for c in self.columns:
            if c.header == header:
                return c
        raise ConfigError(f"template '{self.name}' has no column '{header}'")

    def index_of(self, header: str) -> int:
        return self.headers.index(self.column(header).header)

    def validate(self) -> None:
        if not self.name.strip():
            raise ConfigError("template name is empty")
        if len(set(self.headers)) != len(self.headers):
            raise ConfigError(f"template '{self.name}': duplicate column headers")
        if self.header_row < 0:
            raise ConfigError(f"template '{self.name}': header row must be >= 0")
        bindings: dict[str, dict[Role, str]] = {}
        for c in self.columns:
            c.validate()
            if c.action is Action.REPLACE and c.role in (Role.CODE, Role.NAME):
                roles = bindings.setdefault(c.binding_key, {})
                if c.role in roles:
                    raise ConfigError(
                        f"template '{self.name}': binding '{c.binding_key}' has two {c.role.value} columns"
                    )
                roles[c.role] = c.entity_type
        for key, roles in bindings.items():
            if len(set(roles.values())) > 1:
                raise ConfigError(f"template '{self.name}': binding '{key}' mixes entity types")


@dataclass(frozen=True)
class RelationSpec:
    """ファイル間の関連（例 売上明細.得意先CD → 顧客マスタ.顧客番号）。"""

    from_template: str
    from_column: str
    to_template: str
    to_column: str


@dataclass
class Config:
    version_id: int | None
    templates: dict[str, TemplateSpec] = field(default_factory=dict)
    relations: list[RelationSpec] = field(default_factory=list)

    def template(self, name: str) -> TemplateSpec:
        try:
            return self.templates[name]
        except KeyError:
            raise ConfigError(f"unknown template '{name}'") from None


def normalize_header(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())
