# AI WordPress Factory 學習代理人
# 负責分析人工校稿的修改，提取規律並產生學習提案

from typing import Optional, Dict, Any, List
from state import Task
from contracts import LearningProposal, ProposalStatus
from . import BaseAgent


class LearnerAgent(BaseAgent):
    """學習代理人，負責分析人工校稿的修改，提取規律並產生學習提案。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責分析人工校稿的修改，提取規律並產生學習提案。"

    def analyze_review(self, task: Task) -> Dict[str, Any]:
        """分析人工校稿的修改內容。

        Args:
            task: 任務對象，包含原始草稿和人工修正後內容。

        Returns:
            Dict[str, Any]: 學習分析結果。
        """
        draft_content = task.draft_content or ""
        final_content = task.final_content or draft_content

        if draft_content == final_content:
            return {
                "修改分析": [],
                "學習提案": [],
                "學習總結": "本轮無人工修改，無需更新規則庫。"
            }

        prompt_template = self.get_prompt("learner")
        if not prompt_template:
            prompt_template = """
            你是一位內容風格的學習專家。請分析以下人工校稿的修改：

            原始草稿:
            {draft_content}

            人工修正後內容:
            {final_content}

            當前風格規則:
            {style_rules}

            當前語氣樣本:
            {tone_sample}

            請比較差異，歸納修改規律，並提供學習提案。

            以 JSON 格式返回：
            {{
                "修改分析": [
                    {{
                        "位置": "第X段",
                        "原文": "原文內容",
                        "修改後": "修改後內容",
                        "修改類型": "刪除/修改/新增/重組",
                        "歸納的規律": "一句話描述這個修改反映的寫作規律",
                        "是否值得加入規則庫": true/false,
                        "理由": "為什麼值得或不值得加入"
                    }}
                ],
                "學習提案": [
                    {{
                        "rule": "具體的規則內容",
                        "reason": "為什麼需要這個規則",
                        "source_task": "{task_id}",
                        "status": "pending"
                    }}
                ],
                "學習總結": "一句話總結這次修改反映的核心寫作問題"
            }}
            """

        style_rules = self._load_file("prompts/style-rules.md")
        tone_sample = self._load_file("prompts/tone-sample.md")

        prompt = prompt_template.format(
            draft_content=draft_content,
            final_content=final_content,
            style_rules=style_rules,
            tone_sample=tone_sample,
            task_id=task.id,
        )

        response = self.call_ai(prompt, temperature=0.3, max_tokens=2000)

        import json
        try:
            result = json.loads(response)
            proposals = []
            for p in result.get("學習提案", []):
                proposal = LearningProposal(
                    rule=p.get("rule", ""),
                    reason=p.get("reason", ""),
                    source_task=p.get("source_task", task.id),
                    created_at=Task.__dataclass_fields__["created_at"].default,
                    status=p.get("status", ProposalStatus.PENDING.value),
                )
                proposals.append(proposal.__dict__)
            result["學習提案"] = proposals
            return result
        except json.JSONDecodeError:
            return {
                "修改分析": [],
                "學習提案": [],
                "學習總結": "AI 分析結果解析失敗，請稍後重試。"
            }

    def apply_proposals(self, task: Task, approved_proposals: List[Dict[str, Any]]) -> bool:
        """將已審核的學習提案標記為已批准。

        Args:
            task: 任務對象。
            approved_proposals: 已審核通過的提案列表。

        Returns:
            bool: 更新是否成功。
        """
        if not approved_proposals:
            return False

        updated = False
        for proposal in approved_proposals:
            rule = proposal.get("rule", "")
            if not rule:
                continue
            proposal["status"] = "approved"
            updated = True
        
        return updated

    def flush_approved_rules(self, task: Task) -> bool:
        """將已批准的學習提案寫入風格規則檔案。

        Args:
            task: 任務對象。

        Returns:
            bool: 更新是否成功。
        """
        approved = [p for p in task.learning_proposals if p.get("status") == "approved"]
        if not approved:
            return False

        try:
            with open("prompts/style-rules.md", "a", encoding="utf-8") as f:
                for proposal in approved:
                    rule = proposal.get("rule", "")
                    if rule:
                        f.write(f"\n\n## learnt from review\n\n- {rule}\n")
            return True
        except Exception as e:
            self.log(f"更新風格規則失敗: {str(e)}", "error")
            return False

    def _load_file(self, file_path: str) -> str:
        """讀取文件內容。

        Args:
            file_path: 文件路徑。

        Returns:
            str: 文件內容。
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""
