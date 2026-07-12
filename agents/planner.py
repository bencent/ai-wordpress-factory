# AI WordPress Factory 規劃代理人
# 负責規劃內容的生成流程

from typing import Dict, Any, Optional
from state import Task, ContentType
from . import BaseAgent


class PlannerAgent(BaseAgent):
    """規劃代理人，負責創建內容生成的詳細計劃。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責規劃內容的生成流程，包括主題、結構、關鍵點等。"

    def create_plan(self, task: Task) -> Dict[str, Any]:
        """為任務創建詳細的內容生成計劃。
        
        Args:
            task: 任務對象。
        
        Returns:
            Dict[str, Any]: 內容生成計劃，包含主題、結構、關鍵點等。
        """
        # 獲取提示模板
        prompt_template = self.get_prompt("planner")
        
        if not prompt_template:
            prompt_template = """
            你是一個內容規劃專家。請根據以下任務信息，創建一個詳細的內容生成計劃：
            
            任務標題: {title}
            任務描述: {description}
            内容類型: {content_type}
            
            計劃應包含以下部分：
            1. 主題：內容的核心主題
            2. 目標受眾：內容的目標讀者
            3. 核心信息：需要包含的關鍵信息
            4. 結構大綱：內容的結構大綱
            5. 關鍵字：SEO 相關的關鍵字
            6. 參考資源：需要參考的資源或鏈接
            
            以 JSON 格式返回計劃， 對 key 使用繁體中文。
            """
        
        # 替換提示中的變量
        prompt = prompt_template.format(
            title=task.title,
            description=task.description or "",
            content_type=task.content_type.name,
        )
        
        # 调用 AI 生成計劃
        plan_json = self.call_ai(prompt)
        
        # 解析 JSON 响應
        import json
        try:
            plan = json.loads(plan_json)
            return plan
        except json.JSONDecodeError:
            # 如果解析失敗，返回默認結構
            return {
                "主題": task.title,
                "目標受眾": "一般讀者",
                "核心信息": [task.title],
                "結構大綱": [{"章節": 1, "標題": "引言", "內容": ""}],
                "關鍵字": [task.title],
                "參考資源": [],
            }

    def validate_plan(self, plan: Dict[str, Any]) -> bool:
        """驗證生成的計劃是否有效。
        
        Args:
            plan: 內容生成計劃。
        
        Returns:
            bool: 計劃是否有效。
        """
        required_keys = ["主題", "目標受眾", "核心信息", "結構大綱"]
        return all(key in plan for key in required_keys)
