# AI WordPress Factory 路由代理人
# 负責根據 ReviewResult 決定下一步行動

from typing import Optional, Dict, Any
from state import Task
from contracts import ReviewAction, ReviewResult
from . import BaseAgent


class Router(BaseAgent):
    """路由代理人，根據 ReviewResult 決定下一步行動。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責根據審閱結果決定下一步行動：發布、重寫、研究、SEO 或失敗。"

    def decide(self, review_result: ReviewResult, task: Task) -> str:
        """根據審閱結果決定下一步。

        Args:
            review_result: 審閱結果。
            task: 任務對象。

        Returns:
            str: 下一步行動。
        """
        action = review_result.suggested_action

        if review_result.passed and action == ReviewAction.PUBLISH.value:
            return "publish"

        if action == ReviewAction.REWRITE.value:
            return "rewrite"

        if action == ReviewAction.RESEARCH.value:
            return "research"

        if action == ReviewAction.SEO.value:
            return "seo"

        if action == ReviewAction.FAIL.value:
            return "fail"

        return "rewrite"
