"""画面に出す文言（設計書 04 章。内部用語を使わない。「完全匿名化済み」「安全」等は使わない）。"""

from __future__ import annotations

NOTICE = "名称・コード等の置き換えであり、匿名化を保証するものではありません。"
ORIGINAL_SHOWN = "元の値を表示中"
MASKED_TEXT = "●●●"

STEPS = ["① プロジェクト", "② ファイル取込", "③ 変換設定", "④ 変換前後の確認", "⑤ 検証・出力"]

ACTIONS = {"keep": "維持", "replace": "置換", "clear": "内容を消す", "freetext": "文章内の語句を置換"}
ROLES = {"code": "コード", "name": "名称", "value": "その他の値"}
VALUE_TYPES = {"PHONE": "電話番号", "EMAIL": "メール", "POSTAL": "郵便番号", "ADDRESS": "住所", "URL": "URL"}
OUTSIDE = {"freetext": "文章内の語句を置換", "keep": "そのまま（残存検査の対象）", "clear": "内容を消す"}

REVIEW_KINDS = {
    "new_entity": "新しい対象",
    "code_multi_name": "同じコードに複数の名称",
    "name_ambiguous": "同じ名称の対象が複数",
    "similar_name": "似た名称",
    "residual_short": "短い語の残り",
    "freetext_short": "文章内の短い語",
    "freetext_ambiguous": "文章内の語の置換先",
    "freetext_candidate": "文章内の候補（規則で検出）",
}
CANDIDATE_PATTERNS = {
    "phone": "電話番号らしい語", "email": "メールアドレスらしい語", "postal": "郵便番号らしい語", "url": "URL",
    "company": "法人名らしい語", "honorific": "敬称（様・御中・殿）の付いた語", "person_label": "担当者名らしい語",
    "ai": "AI が示した名称らしい語",
}
DECISIONS = {
    "register_new": "新しい対象として登録",
    "link_existing": "既存の対象に紐付け",
    "add_alias": "別名として登録",
    "keep_as_source_error": "元データの誤りとして扱う",
    "false_positive": "問題なし（置換しない）",
    "replace_term": "選んだ置換先で置換する",
    "register_value": "値として登録して置換する",
}

FINDINGS = {
    # 変換で生じた不備（出力不可）
    "output_unreadable": "出力ファイルを読み直せません",
    "format_changed": "CSV の形式（文字コード・区切り等）が変わりました",
    "record_count_changed": "行数が変わりました",
    "field_count_changed": "列数が変わった行があります",
    "kept_value_changed": "維持した値が変わったセルがあります",
    "unresolved_cell": "未解決のセルがあります",
    "planned_value_mismatch": "計画と違う値のセルがあります",
    "replace_not_applied": "置換されていないセルがあります",
    "clear_not_applied": "内容が消えていないセルがあります",
    "style_changed": "書式が変わったセルがあります",
    "formula_changed": "数式が変わったセルがあります",
    "formula_not_converted": "値に変換されていない数式があります",
    "cache_not_cleared": "計算結果が消えていない数式があります",
    "cell_set_changed": "セルの位置が変わりました",
    "sheet_structure_changed": "シートの構成が変わりました",
    "package_invalid": "Excel ファイルの構造に不整合があります",
    "second_reader_failed": "別の読込方法で出力を読めません",
    "second_reader_sheet_missing": "別の読込方法でシートが見つかりません",
    "second_reader_mismatch": "別の読込方法で読んだ値が一致しません",
    "code_not_consistent": "同じ元コードが異なる値に置換されました",
    "code_not_unique": "異なる元コードが同じ値に置換されました",
    "name_not_consistent": "同じ対象が異なる名称に置換されました",
    "name_not_unique": "異なる対象が同じ名称に置換されました",
    "relation_changed": "ファイル間の関連の結合件数が変わりました",
    "residual": "元の語句が残っています",
    "residual_short": "短い語が残っています（④で判断してください）",
    "residual_sheet_name": "シート名に元の語句が含まれています（Excel でシート名を変えてください）",
    "residual_filename": "出力ファイル名に元の語句が含まれています",
    "open_reviews": "未確認の項目があります",
    # 取込ファイル由来
    "unsupported_feature": "対応していない Excel の機能があります",
    "comments_present": "コメントがあります（「コメントを除去」を選ぶと出力できます）",
    "images_need_confirmation": "画像があります（中身を検査しないことを確認してください）",
    "formula_constant_partial": "数式の文字列に元の語句の一部が含まれています（「数式を値に変換」を選べます）",
    "formula_constant_ambiguous": "数式の文字列の置換先が1つに決まりません（「数式を値に変換」を選べます）",
    "formula_external_ref": "外部のブックを参照する数式があります",
    "formula_unparsable": "解析できない数式があります",
    # 警告・情報
    "formula_text_function": "文字列を加工する数式があります（再計算で結果が変わることがあります）",
    "formula_cache_cleared": "数式の計算結果を消しました（Excel で開くと再計算されます）",
    "formula_converted_to_value": "数式を値に変換しました",
    "formula_constant_replaced": "数式の文字列を置換しました",
    "formula_cache_replaced": "数式の計算結果を置換しました",
    "freetext_replaced": "文章内の語句を置換しました（セル数）",
    "part_text_replaced": "ヘッダー・フッター、グラフの文字を置換しました",
    "ai_checked": "AI で文章を検査しました（候補は④で確認済み）",
    "ai_not_checked": "AI で検査していない文章があります（規則と辞書だけで検査しました）",
    "recalculate_on_open": "Excel で開いたときに再計算する設定にしました（閉じるときに保存の確認が出ることがあります）",
    "rich_text_flattened": "書式付きの文字列を1つの書式にまとめました",
    "images_not_inspected": "画像の中身は検査していません",
    "binary_not_inspected": "プリンタ設定等のバイナリ部品は検査していません",
    "comments_removed": "コメントを除去しました",
    "charts_removed": "グラフを除去しました",
    "charts_present": "グラフがあります（グラフ内の文字も検査します）",
    "part_removed": "縮小画像等を除去しました",
    "properties_reset": "文書のプロパティ（作成者等）を空にしました",
    "hidden_sheet": "非表示のシートがあります（全セルを検査します）",
    "code_capacity_low": "変換後コードの残りが少なくなっています",
    "code_missing": "コードが空欄の行があります（名称で照合しました）",
    "name_multi_code": "同じ名称で別のコードの対象があります（別の対象として扱いました）",
    "relation_unmatched": "元データで参照先にないコードがあります（元データ由来）",
    "relation_duplicate_target": "参照先のコードが重複しています（元データ由来）",
    "relation_not_checked": "関連の一方のファイルが作業に含まれていません",
    "ragged_record": "列数が見出しと違う行があります（元データ由来）",
    "residual_numeric_match": "数値の列で元のコードと同じ数字があります（偶然の一致として扱いました）",
}

ORIGINS = {"transform": "変換で生じた不備", "source": "元データ・元ファイル由来", "environment": "確認できなかった項目"}
SEVERITIES = {"block": "出力不可", "warn": "警告", "info": "情報（参考）"}


def finding_label(kind: str) -> str:
    return FINDINGS.get(kind, kind)
