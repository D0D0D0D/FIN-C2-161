"""過去案件の記述が現在案件の所見として直出しされないことを固定する。

由来: 2026-08-13 の独立レビュー（Batch 3）。レビュアーの再現 probe をそのまま
回帰テストにしている（実装者が書くと実装の写しになるため）。

欠陥: LLM 不在の縮退経路（`_build_fallback`）が、過去記録の `countervailing_notes` を
**現在案件の `countervailing_factors` にそのままコピー**していた。

    status=success
    countervailing_factors=['PAST_CASE_ONLY_MARKER']

過去の裁定は「調査観点」であって現在案件の結論ではない、というのがこの AG の設計前提
（prompt にも `Prior records are investigation perspectives, not conclusions.` と明記）。
過去案件の文言をそのまま現在の反証事情として出すと、その前提が壊れる。

**既存テストがこの誤挙動を正常として assert していた**ため検出できなかった
（`tests/integration/test_batch3_security_composition.py`）。
"""
from __future__ import annotations

from src.nodes.domain_workflow_nodes import _build_fallback

PAST_ONLY = "PAST_CASE_ONLY_MARKER"


def _state_with_prior_perspective() -> dict:
    return {
        "case_ref": "case-1",
        "evidence_check": {"evidence_gap": []},
        "recalled_perspectives": [{
            "source_id": "prior-1",
            "countervailing_notes": [PAST_ONLY],
            "checked_questions": [], "evidence_categories": [],
        }],
        "evidence_registry": [],
    }


def test_prior_case_notes_do_not_become_current_countervailing_factors():
    """過去案件の文言が現在案件の反証事情としてそのまま出ないこと。"""
    draft = _build_fallback(_state_with_prior_perspective())
    assert PAST_ONLY not in draft.get("countervailing_factors", []), (
        "過去案件の countervailing_notes が現在案件の countervailing_factors に直出しされた: "
        f"{draft.get('countervailing_factors')}"
    )


def test_prior_perspectives_are_still_surfaced_somewhere():
    """過去観点を完全に捨てるのではなく、別フィールドか確認質問として残すこと。

    「過去の裁定は組織の事実であり参照してよい」——防ぐのは *現在の結論としての断定* だけ。
    蓄積型 AG の価値そのものを消さないための対。
    """
    draft = _build_fallback(_state_with_prior_perspective())
    surfaced = "\n".join(
        str(v) for k, v in draft.items() if k != "countervailing_factors"
    )
    assert PAST_ONLY in surfaced or draft.get("requires_human_review"), (
        "過去観点がどこにも出ず、人間レビューも要求されていない"
    )
