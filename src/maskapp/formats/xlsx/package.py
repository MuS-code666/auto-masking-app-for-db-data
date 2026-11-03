"""xlsx のパッケージ（ZIP の中の部品）の読込・分類・書込（設計書 06 章 2〜3）。

ライブラリで開いて保存し直す方式は採らず、部品単位で扱う。
- 部品の種類を許可リストと照合し、機能（ピボット・外部リンク・コメント等）を検出する。
- 書き換えない部品は元のバイト列のまま格納する。
- ZIP の時刻は固定値にし、同じ入力から同じ出力を作る。
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field

from lxml import etree

CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN_NS = "http://purl.oclc.org/ooxml/spreadsheetml/main"

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_MAX_PARTS = 10_000
_MAX_UNCOMPRESSED = 2 * 1024**3  # 展開後の合計の上限（ZIP 爆弾対策）

_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=False)


class XlsxFormatError(ValueError):
    """xlsx として処理できない。reason は画面の案内に使う識別子（値を含めない）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_xml(data: bytes) -> etree._Element:
    try:
        return etree.fromstring(data, _XML_PARSER)
    except etree.XMLSyntaxError:
        raise XlsxFormatError("malformed_xml") from None


def serialize_xml(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


# ---------------------------------------------------------------- 部品の分類

# 内容種別の末尾 → (機能, 扱い)
#   keep    維持（XML は残存検査の対象）
#   rewrite 本アプリが書き換える
#   binary  中身を検査しない（一覧表示）
#   image   画像（中身を検査しない。確認または除去）
#   comment コメント（既定は停止、除去を選択可）
#   chart   グラフ（残存がなければ維持、除去を選択可）
#   remove  常に除去
#   block   未対応（停止）
_CT_RULES: list[tuple[str, str, str]] = [
    ("package.relationships+xml", "relationships", "keep"),
    ("package.core-properties+xml", "properties", "rewrite"),
    ("spreadsheetml.sheet.main+xml", "workbook", "rewrite"),
    ("spreadsheetml.template.main+xml", "workbook", "rewrite"),
    ("ms-excel.sheet.macroEnabled.main+xml", "macro", "block"),
    ("ms-excel.template.macroEnabled.main+xml", "macro", "block"),
    ("ms-excel.addin.macroEnabled.main+xml", "macro", "block"),
    ("spreadsheetml.worksheet+xml", "worksheet", "rewrite"),
    ("spreadsheetml.chartsheet+xml", "chartsheet", "block"),
    ("spreadsheetml.dialogsheet+xml", "dialogsheet", "block"),
    ("spreadsheetml.sharedStrings+xml", "shared_strings", "rewrite"),
    ("spreadsheetml.styles+xml", "styles", "keep"),
    ("officedocument.theme+xml", "theme", "keep"),
    ("themeOverride+xml", "theme", "keep"),
    ("spreadsheetml.calcChain+xml", "calc_chain", "keep"),
    ("spreadsheetml.table+xml", "table", "keep"),
    ("spreadsheetml.sheetMetadata+xml", "metadata", "keep"),
    ("spreadsheetml.volatileDependencies+xml", "volatile_dependencies", "keep"),
    ("drawing+xml", "drawing", "keep"),
    ("vmlDrawing", "vml_drawing", "keep"),
    ("drawingml.chart+xml", "chart", "chart"),
    ("drawingml.chartshapes+xml", "chart", "chart"),
    ("ms-office.chartstyle+xml", "chart", "chart"),
    ("ms-office.chartcolorstyle+xml", "chart", "chart"),
    ("spreadsheetml.comments+xml", "comments", "comment"),
    ("ms-excel.threadedcomments+xml", "comments", "comment"),
    ("ms-excel.person+xml", "comments", "comment"),
    ("spreadsheetml.pivotTable+xml", "pivot", "block"),
    ("spreadsheetml.pivotCacheDefinition+xml", "pivot", "block"),
    ("spreadsheetml.pivotCacheRecords+xml", "pivot", "block"),
    ("spreadsheetml.externalLink+xml", "external_link", "block"),
    ("spreadsheetml.connections+xml", "connection", "block"),
    ("spreadsheetml.queryTable+xml", "connection", "block"),
    ("spreadsheetml.revisionHeaders+xml", "revisions", "block"),
    ("spreadsheetml.revisionLog+xml", "revisions", "block"),
    ("spreadsheetml.userNames+xml", "revisions", "block"),
    ("spreadsheetml.slicer+xml", "slicer", "block"),
    ("spreadsheetml.slicerCache+xml", "slicer", "block"),
    ("spreadsheetml.timeline+xml", "slicer", "block"),
    ("spreadsheetml.timelineCache+xml", "slicer", "block"),
    ("ms-excel.controlproperties+xml", "control", "block"),
    ("activeX", "activex", "block"),
    ("vbaProject", "macro", "block"),
    ("oleObject", "ole_object", "block"),
    ("customXmlProperties+xml", "custom_xml", "block"),
    ("printerSettings", "printer_settings", "binary"),
    ("extended-properties+xml", "properties", "rewrite"),
    ("custom-properties+xml", "custom_properties", "remove"),
    ("webextension+xml", "web_extension", "block"),
    ("webextensiontaskpanes+xml", "web_extension", "block"),
    ("application/xml", "xml", "keep"),
]
_IMAGE_TYPES = ("image/",)


@dataclass(frozen=True)
class PartInfo:
    name: str
    content_type: str
    feature: str
    handling: str


def classify(name: str, content_type: str) -> PartInfo:
    lowered = name.lower()
    if content_type.startswith(_IMAGE_TYPES):
        if lowered.startswith("docprops/thumbnail"):
            return PartInfo(name, content_type, "thumbnail", "remove")
        return PartInfo(name, content_type, "image", "image")
    if lowered.startswith("customxml/"):
        return PartInfo(name, content_type, "custom_xml", "block")
    if lowered.startswith("xl/embeddings/"):
        return PartInfo(name, content_type, "ole_object", "block")
    if lowered.startswith("xl/model/"):
        return PartInfo(name, content_type, "data_model", "block")
    if lowered.startswith("xl/richdata/"):
        return PartInfo(name, content_type, "rich_data", "block")
    for suffix, feature, handling in _CT_RULES:
        if suffix in content_type:
            if feature == "xml":
                break  # 汎用の application/xml は場所で判断する
            return PartInfo(name, content_type, feature, handling)
    return PartInfo(name, content_type, "unknown_part", "block")


# ---------------------------------------------------------------- 関係定義


@dataclass
class Relationship:
    id: str
    type: str
    target: str
    external: bool

    @property
    def kind(self) -> str:
        return self.type.rsplit("/", 1)[-1]


def rels_name(part: str) -> str:
    folder, base = posixpath.split(part)
    return posixpath.join(folder, "_rels", base + ".rels")


def source_of_rels(rels: str) -> str:
    folder, base = posixpath.split(rels)
    parent = posixpath.dirname(folder)
    return posixpath.join(parent, base[: -len(".rels")])


def resolve_target(source: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target)).lstrip("./") if target else ""


# ---------------------------------------------------------------- パッケージ


@dataclass
class Package:
    order: list[str]
    data: dict[str, bytes]
    defaults: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def read(cls, raw: bytes) -> Package:
        if raw.startswith(_OLE_MAGIC):
            raise XlsxFormatError("encrypted_or_legacy")  # パスワード付き、または .xls
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise XlsxFormatError("not_a_zip") from None
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > _MAX_PARTS or sum(i.file_size for i in infos) > _MAX_UNCOMPRESSED:
                raise XlsxFormatError("package_too_large")
            order, data = [], {}
            for info in infos:
                name = info.filename
                if name in data:
                    raise XlsxFormatError("duplicate_part")
                try:
                    data[name] = zf.read(info)
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                    raise XlsxFormatError("corrupt_zip_entry") from None
                order.append(name)
        if "[Content_Types].xml" not in data:
            raise XlsxFormatError("missing_content_types")
        pkg = cls(order, data)
        root = parse_xml(data["[Content_Types].xml"])
        for el in root:
            tag = etree.QName(el).localname
            if tag == "Default":
                pkg.defaults[el.get("Extension", "").lower()] = el.get("ContentType", "")
            elif tag == "Override":
                pkg.overrides[el.get("PartName", "").lstrip("/")] = el.get("ContentType", "")
        return pkg

    # ---- 参照

    def content_type(self, name: str) -> str:
        if name in self.overrides:
            return self.overrides[name]
        base = posixpath.basename(name)
        ext = base.rsplit(".", 1)[1].lower() if "." in base else ""  # "_rels/.rels" の拡張子は "rels"
        return self.defaults.get(ext, "")

    def parts(self) -> list[PartInfo]:
        return [classify(n, self.content_type(n)) for n in self.order if n != "[Content_Types].xml"]

    def relationships(self, source: str) -> list[Relationship]:
        """source の関係定義。source が "" ならパッケージ直下（_rels/.rels）。"""
        name = "_rels/.rels" if source == "" else rels_name(source)
        if name not in self.data:
            return []
        rels = []
        for el in parse_xml(self.data[name]):
            external = el.get("TargetMode") == "External"
            target = el.get("Target", "")
            rels.append(Relationship(el.get("Id", ""), el.get("Type", ""),
                                     target if external else resolve_target(source, target), external))
        return rels

    def related(self, source: str, kind: str) -> list[str]:
        return [r.target for r in self.relationships(source) if not r.external and r.kind == kind]

    def main_part(self) -> str:
        for rel in self.relationships(""):
            if rel.kind == "officeDocument" and not rel.external:
                return rel.target
        raise XlsxFormatError("missing_workbook")

    def reachable(self) -> set[str]:
        """パッケージ直下の関係定義からたどれる部品。"""
        seen: set[str] = set()
        stack = [r.target for r in self.relationships("") if not r.external]
        while stack:
            part = stack.pop()
            if part in seen or part not in self.data:
                continue
            seen.add(part)
            stack.extend(r.target for r in self.relationships(part) if not r.external)
        return seen

    # ---- 変更

    def put(self, name: str, data: bytes, content_type: str | None = None) -> None:
        if name not in self.data:
            self.order.append(name)
        self.data[name] = data
        if content_type is not None and self.content_type(name) != content_type:
            self.overrides[name] = content_type

    def remove_parts(self, names: Iterable[str]) -> list[str]:
        """部品を除き、それを指す関係定義も除く。たどれなくなった部品も除き、除いた部品名の一覧を返す。"""
        before = self.reachable()
        targets = {n for n in names if n in self.data}
        removed: list[str] = []
        while targets:
            for name in sorted(targets):
                self._drop(name)
                removed.append(name)
            self._drop_dangling_relationships()
            after = self.reachable()
            targets = {n for n in before if n not in after and n in self.data}
        return removed

    def _drop(self, name: str) -> None:
        self.data.pop(name, None)
        self.overrides.pop(name, None)
        if name in self.order:
            self.order.remove(name)
        rels = rels_name(name)
        if rels in self.data:
            self.data.pop(rels)
            self.order.remove(rels)

    def _drop_dangling_relationships(self) -> None:
        for rels in [n for n in self.order if n.endswith(".rels")]:
            source = "" if rels == "_rels/.rels" else source_of_rels(rels)
            root = parse_xml(self.data[rels])
            changed = False
            for el in list(root):
                if el.get("TargetMode") == "External":
                    continue
                if resolve_target(source, el.get("Target", "")) not in self.data:
                    root.remove(el)
                    changed = True
            if changed:
                self.data[rels] = serialize_xml(root)

    def remove_relationship(self, source: str, rel_id: str) -> None:
        name = "_rels/.rels" if source == "" else rels_name(source)
        root = parse_xml(self.data[name])
        for el in list(root):
            if el.get("Id") == rel_id:
                root.remove(el)
        self.data[name] = serialize_xml(root)

    def add_relationship(self, source: str, rel_type: str, target_part: str) -> str:
        name = "_rels/.rels" if source == "" else rels_name(source)
        if name in self.data:
            root = parse_xml(self.data[name])
        else:
            root = etree.Element(f"{{{REL_NS}}}Relationships", nsmap={None: REL_NS})
        ids = {el.get("Id") for el in root}
        n = 1
        while f"rId{n}" in ids:
            n += 1
        rel_id = f"rId{n}"
        target = posixpath.relpath(target_part, posixpath.dirname(source) or ".")
        etree.SubElement(root, f"{{{REL_NS}}}Relationship", Id=rel_id, Type=rel_type, Target=target)
        self.put(name, serialize_xml(root))
        return rel_id

    # ---- 書込

    def _content_types_xml(self) -> bytes:
        root = etree.Element(f"{{{CT_NS}}}Types", nsmap={None: CT_NS})
        for ext, ct in self.defaults.items():
            etree.SubElement(root, f"{{{CT_NS}}}Default", Extension=ext, ContentType=ct)
        for name in self.order:
            if name in self.overrides:
                etree.SubElement(root, f"{{{CT_NS}}}Override", PartName="/" + name,
                                 ContentType=self.overrides[name])
        return serialize_xml(root)

    def write(self, rewrite_content_types: bool) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for name in self.order:
                if name == "[Content_Types].xml" and rewrite_content_types:
                    data = self._content_types_xml()
                else:
                    data = self.data[name]
                info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                zf.writestr(info, data)
        return buf.getvalue()
