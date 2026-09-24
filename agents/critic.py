# AI WordPress Factory 批判代理人
# 负責自我批評、偵測 AI 寫作模式、挑戰自己的內容

from typing import Optional, Dict, Any, List
from state import Task
from contracts import (
    CritiqueResult,
    CritiqueAction,
    AIPattern,
    AIPatternType,
)
from . import BaseAgent


class CriticAgent(BaseAgent):
    """批判代理人，負責自我批評、偵測 AI 寫作模式、挑戰自己的內容。"""

    def __init__(self, config, *, providers=None):
        super().__init__(config, providers=providers)
        self.description = "負責自我批評、偵測 AI 寫作模式、挑戰內容的假設與品質。"

    def critique(self, task: Task, content: str) -> CritiqueResult:
        """對內容進行自我批評。

        Args:
            task: 任務對象。
            content: 要批評的內容。

        Returns:
            CritiqueResult: 結構化的批評結果。
        """
        prompt_template = self.get_prompt("critic")
        if not prompt_template:
            prompt_template = """
            你是一位嚴格的文章審查者。請對以下文章進行自我批評，挑戰每一個段落、每一個說法。

            文章標題: {title}
            文章內容:
            {content}

            請逐項檢查以下問題：

            1. 這個段落是否真的有資訊價值？
            2. 是否只是換句話說重複前面的內容？
            3. 是否有空泛、沒有證據的描述？
            4. 是否把普通事情說得過度重大？
            5. 是否使用 AI 常見的浮誇語言？
            6. 是否使用制式 AI 句型？
            7. 是否存在英文直譯感？
            8. 是否有不自然的「假真誠」？
            9. 是否存在不必要的排比？
            10. 是否為了看起來完整而加入無意義的段落？
            11. 是否可以直接刪除整段而不影響文章價值？
            12. 是否有真正的觀點，而不是只是整理資訊？
            13. 是否有反方觀點或限制條件被忽略？
            14. 是否過度自信？
            15. 是否有更簡單、更自然的說法？

            另外，請偵測以下 AI 寫作模式：
            - OVERSTATEMENT: 過度誇大（革命性的、劃時代的、顛覆性的）
            - FAKE_SINCERITY: 假真誠（老實說、說真的、坦白說）
            - RULE_OF_THREE: 強制三項並列（A、B、C）
            - FORMULAIC_ENDING: 制式結尾（總結來說、綜上所述、未來展望）
            - AI_TRANSLATION_VOCAB: AI 翻譯詞（深入探索、格局、見證、賦能、解鎖）
            - FAKE_DEPTH: 假深度（不僅是 X，更是 Y）
            - ARTIFICIAL_SCOPE: 虛假範圍（從 X 到 Y，跨度不真實）
            - OVER_SIGNPOSTING: 過度導讀（以下是詳細分析、接下來我們來看看）

            請以 JSON 格式返回，結構如下：
            {{
                "score": 0,
                "issues": ["問題描述"],
                "ai_patterns": [
                    {{
                        "type": "OVERSTATEMENT",
                        "text": "原文文字",
                        "reason": "為什麼這是問題",
                        "suggestion": "如何修正"
                    }}
                ],
                "keep": ["應該保留的段落或句子"],
                "rewrite": [
                    {{
                        "text": "需要修改的文字",
                        "reason": "為什麼要修改",
                        "suggestion": "修改建議"
                    }}
                ],
                "delete": ["應該直接刪除的文字"],
                "research_more": ["需要更多資料的內容"],
                "overall_feedback": "整體評價"
            }}

            嚴格標準：
            - 如果內容很好，就勇敢地寫空陣列/列表
            - 不要為了填滿列表而發明問題
            - 如果文章沒有問題，score 可以給 8-10 分
            - 允許 DELETE：刪除內容是好的，不是失敗
            """

        prompt = prompt_template.format(
            title=task.title,
            content=content,
        )

        response = self.call_ai(prompt, temperature=0.3, max_tokens=4000)

        import json
        try:
            data = json.loads(response)
            patterns = []
            for p in data.get("ai_patterns", []):
                patterns.append(AIPattern(
                    type=p.get("type", "UNKNOWN"),
                    text=p.get("text", ""),
                    reason=p.get("reason", ""),
                    suggestion=p.get("suggestion", ""),
                ))
            return CritiqueResult(
                score=data.get("score", 0),
                issues=data.get("issues", []),
                ai_patterns=patterns,
                keep=data.get("keep", []),
                rewrite=data.get("rewrite", []),
                delete=data.get("delete", []),
                research_more=data.get("research_more", []),
                overall_feedback=data.get("overall_feedback", ""),
            )
        except (json.JSONDecodeError, KeyError):
            return CritiqueResult(
                score=5,
                issues=["Critic 解析失敗，請重試"],
                overall_feedback="批判代理人輸出格式異常。",
            )

    def suggest_action(self, critique: CritiqueResult) -> str:
        """根據批評結果建議下一步行動。

        Args:
            critique: 批評結果。

        Returns:
            str: 建議行動。
        """
        if critique.score >= 8 and not critique.ai_patterns and not critique.issues:
            return CritiqueAction.KEEP.value

        if critique.research_more:
            return CritiqueAction.RESEARCH_MORE.value

        if critique.delete or critique.rewrite or critique.ai_patterns:
            return CritiqueAction.REWRITE.value

        return CritiqueAction.KEEP.value
