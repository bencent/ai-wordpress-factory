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
from contracts import ReviewResult, ReviewAction, CritiqueResult, CritiqueAction
from agents.quality_evaluator import QualityEvaluatorAgent
from agents.content_fixer import ContentFixerAgent
from agents.final_reviewer import FinalReviewerAgent

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
        
        Production Workflow:
        Task -> Planner -> Research -> Writer -> Critic -> SEO -> Reviewer -> Router
            -> ImageAgent -> Publisher -> COMPLETED -> Human Review -> Learner
        
        Args:
            task_id: 任務 ID。
        
        Returns:
            bool: 工作流程是否成功完成。
        """
        task = workflow_state.get_task(task_id)
        if not task:
            logger.error(f"任務不存在: {task_id}")
            return False
        
        try:
            # Step 1: 規劃階段
            workflow_state.update_task_status(task_id, TaskStatus.PLANNING)
            planner = self._get_agent("planner")
            if planner:
                plan = planner.create_plan(task)
                task.plan = plan
                logger.info(f"任務 {task_id} 規劃完成")
            
            # Step 2: 調研階段
            workflow_state.update_task_status(task_id, TaskStatus.RESEARCHING)
            research_agent = self._get_agent("research")
            if research_agent:
                research_data = research_agent.gather_research(task)
                task.research_data = research_data
                logger.info(f"任務 {task_id} 調研完成")
            
            # Step 3: 撰寫階段
            workflow_state.update_task_status(task_id, TaskStatus.WRITING)
            writer = self._get_agent("writer")
            if not writer:
                logger.error("WriterAgent 未配置")
                return False
            draft_content = writer.write_content(task)
            task.draft_content = draft_content
            logger.info(f"任務 {task_id} 撰寫完成")
            
            # Step 4: Self-Critique 階段
            workflow_state.update_task_status(task_id, TaskStatus.CRITIQUING)
            critic = self._get_agent("critic")
            if critic:
                critique_result = critic.critique(task, draft_content)
                task.critique_result = asdict(critique_result)
                logger.info(f"任務 {task_id} 自我批評完成，分數: {critique_result.score}")
                
                if critique_result.delete or critique_result.rewrite or critique_result.research_more:
                    workflow_state.update_task_status(task_id, TaskStatus.REWRITING)
                    revised_content = self._apply_critique(task, draft_content, critique_result)
                    task.revised_content = revised_content
                    logger.info(f"任務 {task_id} 根據批評修改完成")
                else:
                    task.revised_content = draft_content
                    logger.info(f"任務 {task_id} 無需修改")
            else:
                task.revised_content = draft_content
            
            # Step 5: SEO 優化階段
            workflow_state.update_task_status(task_id, TaskStatus.OPTIMIZING)
            seo_agent = self._get_agent("seo")
            if seo_agent:
                optimized_content, seo_metadata = seo_agent.optimize_content(task)
                task.optimized_content = optimized_content
                task.seo_title = seo_metadata.get("title")
                task.seo_description = seo_metadata.get("description")
                task.seo_keywords = seo_metadata.get("keywords")
                logger.info(f"任務 {task_id} SEO 優化完成")
            
            # Step 6: Review 階段（可能進入 retry loop）
            review_passed = False
            review_result = None
            
            while task.retry_count < task.max_retries:
                workflow_state.update_task_status(task_id, TaskStatus.REVIEWING)
                evaluator = self._get_agent("quality_evaluator")
                if evaluator:
                    review_result = evaluator.evaluate(task)
                    logger.info(f"任務 {task_id} 品質評估完成，分數: {review_result.score}")
                
                if review_result and review_result.passed:
                    review_passed = True
                    task.final_content = task.revised_content or task.optimized_content or task.draft_content or ""
                    logger.info(f"任務 {task_id} 審閱通過，分數: {review_result.score}")
                    break
                
                task.retry_count += 1
                logger.warning(f"任務 {task_id} 審閱未通過，重試次數: {task.retry_count}/{task.max_retries}")
                
                if task.retry_count >= task.max_retries:
                    logger.error(f"任務 {task_id} 超過最大重試次數")
                    workflow_state.update_task_status(task_id, TaskStatus.FAILED, error_message="超過最大重試次數")
                    return False
                
                # Router 決策
                workflow_state.update_task_status(task_id, TaskStatus.ROUTING)
                router = self._get_agent("router")
                if router:
                    action = router.decide(review_result, task)
                    logger.info(f"任務 {task_id} Router 決策: {action}")
                    
                    if action == "rewrite":
                        task.draft_content = self._rewrite_content(task, review_result)
                        continue
                    elif action == "research":
                        workflow_state.update_task_status(task_id, TaskStatus.RESEARCHING)
                        research_agent = self._get_agent("research")
                        if research_agent:
                            task.research_data = research_agent.gather_research(task)
                        task.draft_content = self._rewrite_content(task, review_result)
                        continue
                    elif action == "seo":
                        workflow_state.update_task_status(task_id, TaskStatus.OPTIMIZING)
                        seo_agent = self._get_agent("seo")
                        if seo_agent:
                            optimized_content, _ = seo_agent.optimize_content(task)
                            task.optimized_content = optimized_content
                        continue
                    else:
                        break
            
            if not review_passed:
                return False
            
            # 內容修正（審閱未通過時）
            if not review_result.passed and review_result.issues:
                workflow_state.update_task_status(task_id, TaskStatus.REWRITING)
                fixer = self._get_agent("content_fixer")
                if fixer:
                    task.final_content = fixer.fix(task, review_result.issues)
                    logger.info(f"任務 {task_id} 內容修正完成")
                else:
                    task.final_content = task.revised_content or task.optimized_content or task.draft_content or ""
            else:
                task.final_content = task.revised_content or task.optimized_content or task.draft_content or ""
            
            # 最終審核
            workflow_state.update_task_status(task_id, TaskStatus.REVIEWING)
            final_reviewer = self._get_agent("final_reviewer")
            if final_reviewer:
                task.final_content = final_reviewer.final_review(task.final_content, task)
                logger.info(f"任務 {task_id} 最終審核完成")
            
            # Step 7: 人工校稿檢查點
            workflow_state.update_task_status(task_id, TaskStatus.MANUAL_REVIEW)
            self._manual_review_checkpoint(task)
            
            # Step 8: 圖片生成階段
            workflow_state.update_task_status(task_id, TaskStatus.GENERATING_IMAGE)
            image_agent = self._get_agent("image")
            if image_agent and getattr(config, "agents", {}).get("image", {}).get("enabled", True):
                try:
                    media_id, media_url = image_agent.generate_hero_image(task)
                    task.hero_image_id = media_id
                    task.hero_image_url = media_url
                    task.image_status = "success" if media_id else "failed"
                    if media_id:
                        logger.info(f"任務 {task_id} 圖片生成完成: {media_url}")
                    else:
                        logger.warning(f"任務 {task_id} 圖片生成失敗，將繼續發布（無精選圖片）")
                except Exception as e:
                    logger.warning(f"任務 {task_id} 圖片生成異常: {str(e)}")
                    task.image_status = "failed"
            else:
                logger.info(f"任務 {task_id} 圖片生成已禁用，跳過")
            
            # Step 9: 發布階段
            workflow_state.update_task_status(task_id, TaskStatus.PUBLISHING)
            publisher = self._get_agent("publisher")
            if publisher:
                featured_media_id = task.hero_image_id if task.image_status == "success" else None
                wordpress_id, wordpress_url = publisher.publish_content(task, featured_media_id=featured_media_id)
                task.wordpress_id = wordpress_id
                task.wordpress_url = wordpress_url
                logger.info(f"任務 {task_id} 發布完成: {wordpress_url}")
            
            # Step 10: 完成
            workflow_state.update_task_status(task_id, TaskStatus.COMPLETED)
            logger.info(f"任務 {task_id} 已完成")
            
            # Step 11: 學習更新（發布後執行）
            workflow_state.update_task_status(task_id, TaskStatus.LEARNING)
            learner = self._get_agent("learner")
            if learner:
                learning_result = learner.analyze_review(task)
                proposals = learning_result.get("學習提案", [])
                if proposals:
                    task.learning_proposals.extend(proposals)
                    logger.info(f"任務 {task_id} 學習完成：產生 {len(proposals)} 個學習提案")
                else:
                    logger.info(f"任務 {task_id} 學習完成：無需更新規則庫")
            
            return True
            
        except Exception as e:
            logger.error(f"任務 {task_id} 失敗: {str(e)}")
            workflow_state.update_task_status(
                task_id, 
                TaskStatus.FAILED, 
                error_message=str(e)
            )
            return False

    def _get_agent(self, agent_type: str):
        """根據類型獲取代理人實例。

        Args:
            agent_type: 代理人類型。

        Returns:
            代理人實例或 None。
        """
        from agents.planner import PlannerAgent
        from agents.research import ResearchAgent
        from agents.writer import WriterAgent
        from agents.critic import CriticAgent
        from agents.seo import SEOAgent
        from agents.quality_evaluator import QualityEvaluatorAgent
        from agents.content_fixer import ContentFixerAgent
        from agents.final_reviewer import FinalReviewerAgent
        from agents.router import Router
        from agents.image import ImageAgent
        from agents.learner import LearnerAgent
        from tools.wordpress import WordPressPublisher
        
        agents = {
            "planner": PlannerAgent,
            "research": ResearchAgent,
            "writer": WriterAgent,
            "critic": CriticAgent,
            "seo": SEOAgent,
            "reviewer": None,
            "quality_evaluator": QualityEvaluatorAgent,
            "content_fixer": ContentFixerAgent,
            "final_reviewer": FinalReviewerAgent,
            "router": Router,
            "image": ImageAgent,
            "learner": LearnerAgent,
            "publisher": WordPressPublisher,
        }
        
        agent_class = agents.get(agent_type)
        if not agent_class:
            return None
        
        enabled = getattr(config, "agents", {}).get(agent_type, {}).get("enabled", True)
        if not enabled:
            return None
        
        return agent_class(config)

    def _apply_critique(self, task: Task, content: str, critique: CritiqueResult) -> str:
        """根據批評結果修改內容。

        Args:
            task: 任務對象。
            content: 原始內容。
            critique: 批評結果。

        Returns:
            str: 修改後的內容。
        """
        writer = self._get_agent("writer")
        if not writer:
            return content
        
        prompt = f"""
        你是一位專業的內容編輯。請根據以下自我批評結果修改文章：

        原始文章:
        {content}

        批評結果:
        - 分數: {critique.score}
        - 問題: {critique.issues}
        - AI 模式: {critique.ai_patterns}
        - 建議保留: {critique.keep}
        - 建議修改: {critique.rewrite}
        - 建議刪除: {critique.delete}
        - 需要更多資料: {critique.research_more}
        - 整體評價: {critique.overall_feedback}

        修改原則：
        1. 不要為了符合 Critique 而增加更多文字
        2. 如果某段不需要，直接刪除
        3. 如果原本觀點站不住腳，重新論證
        4. 如果沒有證據，不要假裝有證據
        5. 如果內容過度模式化，改變文章組織方式，而不只是換詞

        請返回修改後的完整文章。
        """
        
        revised = writer.call_ai(prompt, temperature=0.7, max_tokens=4000)
        return revised.strip()

    def _rewrite_content(self, task: Task, review_result: ReviewResult) -> str:
        """根據審閱結果重寫內容。

        Args:
            task: 任務對象。
            review_result: 審閱結果。

        Returns:
            str: 重寫後的內容。
        """
        writer = self._get_agent("writer")
        if not writer:
            return task.draft_content or ""
        
        content = task.revised_content or task.optimized_content or task.draft_content or ""
        
        prompt = f"""
        你是一位專業的內容編輯。請根據以下審閱結果重寫文章：

        原始文章:
        {content}

        審閱反饋:
        {review_result.feedback}

        請返回重寫後的完整文章。
        """
        
        rewritten = writer.call_ai(prompt, temperature=0.7, max_tokens=4000)
        return rewritten.strip()

    def _manual_review_checkpoint(self, task: Task) -> None:
        """人工校稿檢查點。暫停工作流程，等待人類審閱和修正。

        Args:
            task: 任務對象。
        """
        content = task.final_content or task.revised_content or task.optimized_content or task.draft_content or ""
        
        print("\n" + "=" * 60)
        print(f"人工校稿檢查點：任務「{task.title}」")
        print("=" * 60)
        print("\n【AI 草稿內容】\n")
        print(content[:2000] + ("..." if len(content) > 2000 else ""))
        print("\n" + "=" * 60)
        print("請審閱以上內容。")
        print("- 直接貼上修正後的完整內容")
        print("- 或輸入 '.' 表示接受原稿")
        print("- 或輸入 'skip' 跳過校稿（不建議）")
        print("=" * 60)
        
        try:
            user_input = input("\n請輸入修正內容: ").strip()
        except EOFError:
            logger.warning("無法讀取使用者輸入，使用 AI 草稿")
            return
        
        if user_input == ".":
            logger.info("使用者接受原稿")
            return
        
        if user_input.lower() == "skip":
            logger.warning("使用者跳過校稿")
            return
        
        if user_input:
            task.final_content = user_input
            logger.info("使用者提供了修正內容")

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
