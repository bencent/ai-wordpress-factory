# AI WordPress Factory 調研代理人
# 负責收集和整理與任務相關的資料

from typing import List, Dict, Any, Optional
from state import Task
from . import BaseAgent


class ResearchAgent(BaseAgent):
    """調研代理人，負責收集和整理與任務相關的資料。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責收集和整理與任務相關的資料，包括網路搜索、數據庫查詢等。"

    def gather_research(self, task: Task) -> List[Dict[str, Any]]:
        """收集與任務相關的資料。
        
        Args:
            task: 任務對象。
        
        Returns:
            List[Dict[str, Any]]: 資料列表，每個資料包含標題、內容、來源等字段。
        """
        research_data = []
        
        # 1. 使用任務的計劃作為參考
        if task.plan:
            for resource in task.plan.get("參考資源", []):
                research_data.append({
                    "類型": "參考資源",
                    "標題": resource.get("標題", "未知"),
                    "內容": resource.get("內容", ""),
                    "來源": resource.get("來源", "計劃"),
                    "相關性": "高",
                })
        
        # 2. 使用搜索工具收集更多資料
        if hasattr(self.config, "search_api_key") and self.config.search_api_key:
            search_results = self._search_web(task)
            research_data.extend(search_results)
        
        # 3. 如果資料不足，使用 AI 生成模擬資料
        if len(research_data) < 3:
            simulated_data = self._generate_simulated_research(task)
            research_data.extend(simulated_data)
        
        return research_data

    def _search_web(self, task: Task) -> List[Dict[str, Any]]:
        """使用搜索 API 收集資料。
        
        Args:
            task: 任務對象。
        
        Returns:
            List[Dict[str, Any]]: 搜索結果列表。
        """
        # 這裡可以集成 Google Custom Search、Bing Search 等 API
        # 目前返回模擬數據
        import random
        
        search_terms = [task.title]
        if task.plan:
            search_terms.extend(task.plan.get("關鍵字", []))
        
        results = []
        for term in search_terms[:3]:  # 搜索前 3 個關鍵字
            results.append({
                "類型": "網路搜索",
                "標題": f"搜索結果：{term}",
                "內容": f"這是關於 {term} 的搜索結果內容。包含了詳細的信息和相關數據。",
                "來源": f"https://example.com/search?q={term}",
                "相關性": random.choice(["高", "中", "低"]),
            })
        
        return results

    def _generate_simulated_research(self, task: Task) -> List[Dict[str, Any]]:
        """使用 AI 生成模擬資料（用於測試或搜索 API 不可用時）。
        
        Args:
            task: 任務對象。
        
        Returns:
            List[Dict[str, Any]]: 模擬資料列表。
        """
        prompt = f"""
        你是一個資料收集專家。請為以下任務生成 3 條相關的資料：
        
        任務標題: {task.title}
        任務描述: {task.description or ""}
        
        每條資料應包含以下字段：
        - 類型：資料類型（如 "背景資料"、"統計數據"、"案例分析" 等）
        - 標題：資料的標題
        - 内容：資料的詳細內容（2-3 句）
        - 來源：資料來源（可以是模擬的 URL 或名稱）
        - 相關性：資料的相關性（高、中、低）
        
        以 JSON 陣列格式返回。
        """
        
        response = self.call_ai(prompt)
        
        import json
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return [
                {
                    "類型": "背景資料",
                    "標題": f"關於 {task.title} 的背景資料",
                    "內容": f"這是關於 {task.title} 的背景資料，提供了基礎的信息和概述。",
                    "來源": f"https://example.com/background-{task.title}",
                    "相關性": "高",
                },
                {
                    "類型": "統計數據",
                    "標題": f"{task.title} 的統計數據",
                    "內容": f"這是 {task.title} 的相關統計數據，包括木有數字、趨勢和分析。",
                    "來源": f"https://example.com/stats-{task.title}",
                    "相關性": "中",
                },
                {
                    "類型": "案例分析",
                    "標題": f"{task.title} 的案例分析",
                    "內容": f"這是一個關於 {task.title} 的案例分析，展示了實際應用和效果。",
                    "來源": f"https://example.com/case-study-{task.title}",
                    "相關性": "中",
                },
            ]
