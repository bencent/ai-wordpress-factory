# AI WordPress Factory 系統入口
# 協調各個模組的工作流程

import logging
from typing import Optional, Dict, Any
from dataclasses import asdict
import json
import uuid
import datetime

from config import config, load_config_from_file
from state import (
    workflow_state,
    Task,
    TaskStatus,
    ContentType,
    WorkflowState,
)

# 配置日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ai_wordpress_factory")


class AIWordPressFactory:
    """AI WordPress Factory 主類，協調所有模組的工作流程。"""

    def __init__(self, config_path: Optional[str] = None):
        """初始化工廠。
        
        Args:
            config_path: 配置文件路徑（可選）。
        """
        # 加載配置
        if config_path:
            global config
            config = load_config_from_file(config_path)
        else:
            from config import load_config_from_env
            load_config_from_env()
        
        logger.info("AI WordPress Factory 初始化完成")

    def create_task(
        self,
        title: str,
        description: Optional[str] = None,
        content_type: ContentType = ContentType.BLOG_POST,
        priority: int = 0,
    ) -> str:
        """創建新任務。
        
        Args:
            title: 任務標題。
            description: 任務描述（可選）。
            content_type: 內容類型（默認為博客文章）。
            priority: 任務優先級（默認為 0）。
        
        Returns:
            str: 新任務的 ID。
        """
        task_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            title=title,
            description=description,
            content_type=content_type,
            priority=priority,
        )
        workflow_state.add_task(task)
        logger.info(f"創建新任務: {title} (ID: {task_id})")
        return task_id

    def run_workflow(self, task_id: str) -> bool:
        """執行工作流程。
        
        Args:
            task_id: 任務 ID。
        
        Returns:
            bool: 工作流程是否成功完成。
        """
        from agents.planner import PlannerAgent
        from agents.research import ResearchAgent
        from agents.writer import WriterAgent
        from agents.seo import SEOAgent
        from agents.reviewer import ReviewerAgent
        from tools.wordpress import WordPressPublisher
        
        task = workflow_state.get_task(task_id)
        if not task:
            logger.error(f"任務不存在: {task_id}")
            return False
        
        try:
            # 1. 規劃階段
            workflow_state.update_task_status(task_id, TaskStatus.PLANNING)
            planner = PlannerAgent(config)
            plan = planner.create_plan(task)
            task.plan = plan
            logger.info(f"任務 {task_id} 規劃完成")
            
            # 2. 調研階段
            workflow_state.update_task_status(task_id, TaskStatus.RESEARCHING)
            research_agent = ResearchAgent(config)
            research_data = research_agent.gather_research(task)
            task.research_data = research_data
            logger.info(f"任務 {task_id} 調研完成")
            
            # 3. 撰寫階段
            workflow_state.update_task_status(task_id, TaskStatus.WRITING)
            writer = WriterAgent(config)
            draft_content = writer.write_content(task)
            task.draft_content = draft_content
            logger.info(f"任務 {task_id} 撰寫完成")
            
            # 4. SEO 優化階段
            workflow_state.update_task_status(task_id, TaskStatus.OPTIMIZING)
            seo_agent = SEOAgent(config)
            optimized_content, seo_metadata = seo_agent.optimize_content(task)
            task.optimized_content = optimized_content
            task.seo_title = seo_metadata.get("title")
            task.seo_description = seo_metadata.get("description")
            task.seo_keywords = seo_metadata.get("keywords")
            logger.info(f"任務 {task_id} SEO 優化完成")
            
            # 5. 審閱階段
            workflow_state.update_task_status(task_id, TaskStatus.REVIEWING)
            reviewer = ReviewerAgent(config)
            final_content = reviewer.review_content(task)
            task.final_content = final_content
            logger.info(f"任務 {task_id} 審閱完成")
            
            # 6. 發布階段
            workflow_state.update_task_status(task_id, TaskStatus.PUBLISHING)
            publisher = WordPressPublisher(config)
            wordpress_id, wordpress_url = publisher.publish_content(task)
            task.wordpress_id = wordpress_id
            task.wordpress_url = wordpress_url
            logger.info(f"任務 {task_id} 發布完成: {wordpress_url}")
            
            # 7. 完成
            workflow_state.update_task_status(task_id, TaskStatus.COMPLETED)
            logger.info(f"任務 {task_id} 已完成")
            return True
            
        except Exception as e:
            logger.error(f"任務 {task_id} 失敗: {str(e)}")
            workflow_state.update_task_status(
                task_id, 
                TaskStatus.FAILED, 
                error_message=str(e)
            )
            return False

    def save_state(self, file_path: str = "workflow_state.json") -> None:
        """保存工作流程狀態到文件。
        
        Args:
            file_path: 保存文件的路徑（默認為 "workflow_state.json"）。
        """
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(workflow_state.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"工作流程狀態已保存到 {file_path}")

    def load_state(self, file_path: str = "workflow_state.json") -> None:
        """從文件加載工作流程狀態。
        
        Args:
            file_path: 文件路徑（默認為 "workflow_state.json"）。
        """
        global workflow_state
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            workflow_state = WorkflowState.from_dict(data)
            logger.info(f"工作流程狀態已從 {file_path} 加載")
        except FileNotFoundError:
            logger.warning(f"文件 {file_path} 不存在，使用默認狀態")


if __name__ == "__main__":
    # 示例用法
    import argparse
    
    parser = argparse.ArgumentParser(description="AI WordPress Factory")
    parser.add_argument("--config", type=str, default=None, help="配置文件路徑")
    parser.add_argument("--task", type=str, default=None, help="任務標題")
    parser.add_argument("--description", type=str, default=None, help="任務描述")
    parser.add_argument("--content-type", type=str, default="BLOG_POST", 
                        help="內容類型 (BLOG_POST, PAGE, PRODUCT)")
    parser.add_argument("--load-state", type=str, default=None, help="加載狀態文件路徑")
    parser.add_argument("--save-state", type=str, default=None, help="保存狀態文件路徑")
    
    args = parser.parse_args()
    
    # 初始化工廠
    factory = AIWordPressFactory(config_path=args.config)
    
    # 加載狀態
    if args.load_state:
        factory.load_state(args.load_state)
    
    # 如果提供了任務，則創建並執行
    if args.task:
        content_type = ContentType[args.content_type.upper()]
        task_id = factory.create_task(
            title=args.task,
            description=args.description,
            content_type=content_type,
        )
        print(f"已創建任務: {task_id}")
        print(f"開始執行工作流程...")
        success = factory.run_workflow(task_id)
        print(f"工作流程執行 {'成功' if success else '失敗'}")
    
    # 保存狀態
    if args.save_state:
        factory.save_state(args.save_state)
