"""変換設定の下書き（設計書 04 章 0「自動保存」、4章）。

画面で列設定・関連を変えるたびに下書きとして project.db（ui_state）に保存し、アプリを閉じても
同じところから再開できるようにする。下書きは「設定を確定」したときに新しい設定版として保存する。
列ごとの確認状態（未確認／確認済み）も下書きに持つ。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from maskapp.config import Config, RelationSpec, TemplateSpec, load_config, save_config
from maskapp.project import Project

_KEY = "config_draft"


@dataclass
class ConfigDraft:
    templates: dict[str, TemplateSpec] = field(default_factory=dict)
    relations: list[RelationSpec] = field(default_factory=list)
    confirmed: dict[str, set[str]] = field(default_factory=dict)  # 表の定義名 → 確認済みの列名
    rejected: set[tuple[str, str, str, str]] = field(default_factory=set)  # 却下した関連の候補
    base_version: int | None = None  # 下書きの元にした設定版
    changed: bool = False  # 設定版から変更があるか

    # ---- 保存 ----

    def to_json(self) -> dict:
        return {
            "templates": [t.to_json() for t in self.templates.values()],
            "relations": [[r.from_template, r.from_column, r.to_template, r.to_column] for r in self.relations],
            "confirmed": {k: sorted(v) for k, v in self.confirmed.items()},
            "rejected": sorted(list(r) for r in self.rejected),
            "base_version": self.base_version,
            "changed": self.changed,
        }

    @classmethod
    def from_json(cls, data: dict) -> ConfigDraft:
        templates = [TemplateSpec.from_json(t) for t in data.get("templates", [])]
        return cls(
            {t.name: t for t in templates},
            [RelationSpec(*r) for r in data.get("relations", [])],
            {k: set(v) for k, v in data.get("confirmed", {}).items()},
            {tuple(r) for r in data.get("rejected", [])},
            data.get("base_version"),
            data.get("changed", False),
        )

    @classmethod
    def load(cls, project: Project) -> ConfigDraft:
        row = project.conn.execute("SELECT value FROM ui_state WHERE key = ?", (_KEY,)).fetchone()
        if row:
            return cls.from_json(json.loads(row[0]))
        config = load_config(project.conn)
        return cls.of_config(config)

    @classmethod
    def of_config(cls, config: Config) -> ConfigDraft:
        return cls(
            dict(config.templates), list(config.relations),
            {name: set(t.headers) for name, t in config.templates.items()}, set(), config.version_id, False,
        )

    def save(self, project: Project) -> None:
        with project.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ui_state (key, value, updated_at) VALUES (?, ?, ?)",
                (_KEY, json.dumps(self.to_json(), ensure_ascii=False),
                 datetime.now(UTC).isoformat(timespec="milliseconds")),
            )

    def publish(self, project: Project) -> int:
        """下書きを新しい設定版として保存する。"""
        with project.transaction() as conn:
            version = save_config(conn, list(self.templates.values()), self.relations)
        self.base_version = version
        self.changed = False
        self.save(project)
        return version

    # ---- 編集 ----

    def put_template(self, template: TemplateSpec, confirmed: set[str] | None = None) -> None:
        self.templates[template.name] = template
        if confirmed is not None:
            self.confirmed[template.name] = set(confirmed) & set(template.headers)
        else:
            self.confirmed[template.name] = self.confirmed.get(template.name, set()) & set(template.headers)
        self.relations = [
            r for r in self.relations
            if (r.from_template != template.name or r.from_column in template.headers)
            and (r.to_template != template.name or r.to_column in template.headers)
        ]
        self.changed = True

    def remove_template(self, name: str) -> None:
        self.templates.pop(name, None)
        self.confirmed.pop(name, None)
        self.relations = [r for r in self.relations if name not in (r.from_template, r.to_template)]
        self.changed = True

    def set_confirmed(self, name: str, header: str, confirmed: bool) -> None:
        s = self.confirmed.setdefault(name, set())
        (s.add if confirmed else s.discard)(header)

    def confirm_all(self, name: str) -> None:
        self.confirmed[name] = set(self.templates[name].headers)

    def unconfirmed(self, names: list[str] | None = None) -> list[tuple[str, str]]:
        out = []
        for name, t in self.templates.items():
            if names is not None and name not in names:
                continue
            done = self.confirmed.get(name, set())
            out += [(name, h) for h in t.headers if h not in done]
        return out

    def add_relation(self, rel: RelationSpec) -> None:
        if rel not in self.relations:
            self.relations.append(rel)
            self.changed = True
        self.rejected.discard((rel.from_template, rel.from_column, rel.to_template, rel.to_column))

    def remove_relation(self, rel: RelationSpec) -> None:
        if rel in self.relations:
            self.relations.remove(rel)
            self.changed = True

    def reject_relation(self, rel: RelationSpec) -> None:
        self.rejected.add((rel.from_template, rel.from_column, rel.to_template, rel.to_column))

    def config(self) -> Config:
        return Config(self.base_version, dict(self.templates), list(self.relations))
