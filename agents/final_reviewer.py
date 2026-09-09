# AI WordPress Factory 最終審核代理人
# 负責最終審核，檢查是否違反風格規則和 AI 寫作模式

from typing import Optional, Dict, Any, List
from state import Task
from . import BaseAgent


class FinalReviewerAgent(BaseAgent):
    """最終審核代理人，負責檢查內容是否違反風格規則和 AI 寫作模式。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責最終審核，檢查內容是否違反風格規則和 AI 寫作模式。"

    def final_review(self, content: str, task: Task) -> str:
        style_rules = self._load_file("prompts/style-rules.md")
        anti_ai = self._load_file("prompts/anti-ai-writing.md")
        
        if not style_rules and not anti_ai:
            return content

        prompt = f"""
        你是一位嚴格的最終審核專家。請檢查以下文章是否違反了任何風格規則或 AI 寫作模式。

        文章內容:
        {content}

        風格規則:
        {style_rules}

        Anti-AI Writing Patterns:
        {anti_ai}

        審核要求：
        1. 逐條檢查文章是否違反了風格規則中的任何禁止事項
        2. 檢查是否有 AI 寫作模式（假真誠、過度誇大、制式結尾等）
        3. 檢查是否有過度使用的句式模式
        4. 檢查文體是否符合基本要求（直接、節奏變化、信任讀者）
        5. 如果發現問題，直接返回修正後的完整文章
        6. 如果沒有發現問題，直接返回原文

        請直接返回修正後的完整文章或原文，不要加任何解釋。
        """

        reviewed_content = self.call_ai(prompt, temperature=0.2, max_tokens=4000)
        return reviewed_content.strip()

    def _load_file(self, file_path: str) -> str:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""
