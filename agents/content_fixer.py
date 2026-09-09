# AI WordPress Factory 內容修正代理人
# 负責修正內容中的品質問題

from typing import Optional, Dict, Any, List
from state import Task
from . import BaseAgent


class ContentFixerAgent(BaseAgent):
    """內容修正代理人，負責修正內容中的品質問題。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責修正內容中的品質問題。"

    def fix(self, task: Task, issues: List[str]) -> str:
        content = task.revised_content or task.optimized_content or task.draft_content or ""
        if not issues:
            return content
        issues_str = "\n".join([f"- {issue}" for issue in issues])
        prompt = f"""
        你是一位專業的內容編輯。請修復以下文章中的問題：

        文章內容:
        {content}

        需要修復的問題：
        {issues_str}

        請返回修改後的完整文章。確保修改後的文章保持原意，只是修復了問題。
        """
        fixed_content = self.call_ai(
            prompt.format(content=content, issues_str=issues_str),
            temperature=0.3,
        )
        return fixed_content
