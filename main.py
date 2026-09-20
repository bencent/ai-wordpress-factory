# AI WordPress Factory 系統入口
# 協調各個模組的工作流程

import logging
from typing import Optional, Dict, Any
from dataclasses import asdict
from copy import deepcopy
from contextlib import contextmanager
from threading import Lock
from domain.observer import WorkflowEvent, WorkflowObserver, NoOpObserver, ObserverError, observed_workflow
import json
import uuid
import datetime
import sys
from pathlib import Path

from config import Config, config, load_config_from_file
from domain.ai_runtime import RunContext, ProviderBundle, classify_error
from domain.agent_settings import agent_settings
from providers.composition import legacy_provider_bundle, legacy_media_upload
from state import (
    workflow_state,
    Task,
    TaskStatus,
    ContentType,
    WorkflowState,
)
from contracts import (
    ReviewResult, ReviewAction, CritiqueResult, CritiqueAction,
    FrontendRequest, FrontendResult,
    FrontendSecurityResult, GreenLightConversionResult, FrontendValidationResult,
    FrontendProductionQualityResult,
    FrontendGate, FrontendFailureFeedback, FrontendFailureSeverity,
    ApprovalPolicyMode, ApprovalPolicy, ClientProfile,
    BrandProductionRules,
    PreviewArtifact, PreviewInfrastructureFailure, FailureCategory,
    VisualQualityResult, VisualQualityAction,
    ImageArtifact, ImageArtifactStatus,
    create_image_artifact,
)
from agents.quality_evaluator import QualityEvaluatorAgent
from agents.content_fixer import ContentFixerAgent
from agents.final_reviewer import FinalReviewerAgent
from agents.frontend import FrontendAgent
from agents.visual_quality import VisualQualityReviewer
from tools.frontend_security import FrontendSecurityGate
from tools.greenlight_converter import GreenLightConverter
from tools.frontend_validator import FrontendValidator
from tools.frontend_production_gate import FrontendProductionQualityGate
from tools.preview_renderer import PreviewRenderer, PreviewRenderError
from tools.rendered_technical_validator import RenderedTechnicalValidator

# 配置日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ai_wordpress_factory")


class AIWordPressFactory:
    """AI WordPress Factory 主類，協調所有模組的工作流程。"""

    def __init__(self, config_path: Optional[str] = None, *,
                 runtime_config: Optional[Config] = None, state: Optional[WorkflowState] = None,
                 providers: Optional[ProviderBundle] = None):
        """Legacy CLI by default; explicit config/state selects isolated execution.

        Injected values are copied so agents cannot mutate the caller or another run.
        Use for_run() to construct a fresh single-task context for service execution.
        """
        self._isolated = runtime_config is not None or state is not None
        self._run_lock = Lock()
        self._observer = NoOpObserver()
        self._observing_task = None
        self._workflow_id = None
        self._sequence = 0
        if self._isolated:
            if config_path is not None:
                raise ValueError("Do not combine config_path with injected run context")
            self._config = deepcopy(runtime_config if runtime_config is not None else Config())
            self._state = deepcopy(state) if state is not None else WorkflowState()
        else:
            if config_path:
                global config
                config = load_config_from_file(config_path)
            else:
                from config import load_config_from_env
                load_config_from_env()
        self.providers = providers if providers is not None else legacy_provider_bundle(self.runtime_config)
        self.run_context = None
        logger.info("AI WordPress Factory 初始化完成")

    @classmethod
    def for_run(cls, task: Task, runtime_config: Config, *, providers=None,
                workspace_id=None, run_id=None, observer=None):
        """Fresh context for one execution; no global state or config loader mutation."""
        state = WorkflowState()
        state.add_task(task)
        factory = cls(runtime_config=runtime_config, state=state, providers=providers)
        factory.run_context = RunContext(workspace_id or "legacy-local",task.id,run_id or str(uuid.uuid4()),
                                         factory.state,observer or NoOpObserver(),factory.providers)
        return factory

    @property
    def state(self):
        return self._state if self._isolated else workflow_state

    @property
    def runtime_config(self):
        return self._config if self._isolated else config

    def _event_snapshot(self, task):
        return deepcopy(task.to_dict())

    def _workflow_error(self, error):
        return classify_error(error).safe_summary

    def _emit(self, event_type, task, stage=None):
        if self._observing_task is None or isinstance(self._observer, NoOpObserver):
            return
        self._sequence += 1
        try:
            event = WorkflowEvent(
                event_id=f"{self._workflow_id}:{self._sequence}",
                workflow_id=self._workflow_id, sequence_number=self._sequence,
                task_id=task.id, type=event_type, stage=stage,
                created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                snapshot=self._event_snapshot(task),
            )
            self._observer.on_event(event)
        except Exception:
            raise ObserverError("Workflow observer failed") from None

    @contextmanager
    def _agent_step(self, task, name):
        self._emit("agent_started", task, name)
        try:
            yield
        except ObserverError:
            raise
        except Exception:
            self._emit("agent_failed", task, name)
            raise
        else:
            self._emit("agent_completed", task, name)
            self._emit("checkpoint_produced", task, name)

    def _observe_workflow(self, operation, task_id: str, observer: Optional[WorkflowObserver] = None) -> bool:
        """Retain legacy bool results, including False when awaiting approval.

        Observer errors propagate and stop execution. Checkpoints are detached
        internal task snapshots, not public API responses or recovery guarantees.
        """
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Factory is already running")
        task = self.state.get_task(task_id)
        try:
            if task is None:
                return False
            self.state.set_current_task(task_id)
            self._observer = observer if observer is not None else (self.run_context.observer if self.run_context else NoOpObserver())
            self._observing_task = task
            self._workflow_id = str(uuid.uuid4())
            self._sequence = 0
            self._emit("workflow_started", task)
            result = operation(self, task_id)
            self._emit("checkpoint_produced", task)
            if task.status == TaskStatus.AWAITING_APPROVAL:
                self._emit("awaiting_approval_reached", task)
                self._emit("workflow_completed", task)
            elif result:
                self._emit("workflow_completed", task)
            else:
                self._emit("workflow_failed", task)
            return result
        except ObserverError:
            if task is not None:
                self.state.update_task_status(task_id, TaskStatus.FAILED_NEEDS_ATTENTION,
                                              error_message="Workflow observer failed")
            raise
        finally:
            self._observer = NoOpObserver()
            self._observing_task = None
            self._run_lock.release()

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
        self.state.add_task(task)
        logger.info(f"創建新任務: {title} (ID: {task_id})")
        return task_id

    @observed_workflow
    def run_workflow(self, task_id: str, observer: Optional[WorkflowObserver] = None) -> bool:
        """執行工作流程。
        
        Production Workflow:
        Task -> Planner -> Research -> Writer -> Critic -> SEO -> Reviewer -> Router
            -> ImageAgent -> Publisher -> COMPLETED -> Human Review -> Learner
        
        Args:
            task_id: 任務 ID。
        
        Returns:
            bool: 工作流程是否成功完成。
        """
        task = self.state.get_task(task_id)
        if not task:
            logger.error(f"任務不存在: {task_id}")
            return False
        
        try:
            # Step 1: 規劃階段
            self.state.update_task_status(task_id, TaskStatus.PLANNING)
            planner = self._get_agent("planner")
            if planner:
                with self._agent_step(task, "planner"):
                    plan = planner.create_plan(task)
                    task.plan = plan
                logger.info(f"任務 {task_id} 規劃完成")
            
            # Step 2: 調研階段
            self.state.update_task_status(task_id, TaskStatus.RESEARCHING)
            research_agent = self._get_agent("research")
            if research_agent:
                with self._agent_step(task, "research"):
                    research_data = research_agent.gather_research(task)
                    task.research_data = research_data
                logger.info(f"任務 {task_id} 調研完成")
            
            # Step 3: 撰寫階段
            self.state.update_task_status(task_id, TaskStatus.WRITING)
            writer = self._get_agent("writer")
            if not writer:
                logger.error("WriterAgent 未配置")
                return False
            with self._agent_step(task, "writer"):
                draft_content = writer.write_content(task)
                task.draft_content = draft_content
            logger.info(f"任務 {task_id} 撰寫完成")
            
            # Step 4: Self-Critique 階段
            self.state.update_task_status(task_id, TaskStatus.CRITIQUING)
            critic = self._get_agent("critic")
            if critic:
                with self._agent_step(task, "critic"):
                    critique_result = critic.critique(task, draft_content)
                    task.critique_result = asdict(critique_result)
                logger.info(f"任務 {task_id} 自我批評完成，分數: {critique_result.score}")
                
                if critique_result.delete or critique_result.rewrite or critique_result.research_more:
                    self.state.update_task_status(task_id, TaskStatus.REWRITING)
                    revised_content = self._apply_critique(task, draft_content, critique_result)
                    task.revised_content = revised_content
                    logger.info(f"任務 {task_id} 根據批評修改完成")
                else:
                    task.revised_content = draft_content
                    logger.info(f"任務 {task_id} 無需修改")
            else:
                task.revised_content = draft_content
            
            # Step 5: SEO 優化階段
            self.state.update_task_status(task_id, TaskStatus.OPTIMIZING)
            seo_agent = self._get_agent("seo")
            if seo_agent:
                with self._agent_step(task, "seo"):
                    optimized_content, seo_metadata = seo_agent.optimize_content(task)
                    task.optimized_content = optimized_content
                    task.seo_title = seo_metadata.get("title")
                    task.seo_description = seo_metadata.get("description")
                    task.seo_keywords = seo_metadata.get("keywords")
                logger.info(f"任務 {task_id} SEO 優化完成")
            
            # Step 6: Review 階段（可能進入 retry loop）
            review_passed = False
            review_result = None
            
            # Initial evaluation is mandatory; the budget counts only subsequent retries.
            while True:
                self.state.update_task_status(task_id, TaskStatus.REVIEWING)
                evaluator = self._get_agent("quality_evaluator")
                if evaluator:
                    with self._agent_step(task, "quality_evaluator"):
                        review_result = evaluator.evaluate(task)
                        task.quality_result = asdict(review_result)
                    logger.info(f"任務 {task_id} 品質評估完成，分數: {review_result.score}")
                
                if review_result and review_result.passed:
                    review_passed = True
                    task.final_content = task.revised_content or task.optimized_content or task.draft_content or ""
                    logger.info(f"任務 {task_id} 審閱通過，分數: {review_result.score}")
                    break
                
                logger.warning(f"任務 {task_id} 審閱未通過，已重試次數: {task.retry_count}/{task.max_retries}")
                
                if task.retry_count >= task.max_retries:
                    logger.error(f"任務 {task_id} 超過最大重試次數")
                    self.state.update_task_status(task_id, TaskStatus.FAILED, error_message="超過最大重試次數")
                    return False
                
                # Router 決策
                self.state.update_task_status(task_id, TaskStatus.ROUTING)
                router = self._get_agent("router")
                if router:
                    with self._agent_step(task, "router"):
                        action = router.decide(review_result, task)
                    logger.info(f"任務 {task_id} Router 決策: {action}")
                    
                    if action in ("rewrite", "research", "seo"):
                        task.retry_count += 1
                    if action == "rewrite":
                        task.draft_content = self._rewrite_content(task, review_result)
                        task.revised_content = task.optimized_content = task.draft_content
                        continue
                    elif action == "research":
                        self.state.update_task_status(task_id, TaskStatus.RESEARCHING)
                        research_agent = self._get_agent("research")
                        if research_agent:
                            with self._agent_step(task, "research"):
                                task.research_data = research_agent.gather_research(task)
                        task.draft_content = self._rewrite_content(task, review_result)
                        task.revised_content = task.optimized_content = task.draft_content
                        continue
                    elif action == "seo":
                        self.state.update_task_status(task_id, TaskStatus.OPTIMIZING)
                        seo_agent = self._get_agent("seo")
                        if seo_agent:
                            with self._agent_step(task, "seo"):
                                optimized_content, _ = seo_agent.optimize_content(task)
                                task.optimized_content = optimized_content
                                task.revised_content = optimized_content
                        continue
                    else:
                        break
                else:
                    break
            
            if not review_passed:
                return False
            
            # 內容修正（審閱未通過時）
            if not review_result.passed and review_result.issues:
                self.state.update_task_status(task_id, TaskStatus.REWRITING)
                fixer = self._get_agent("content_fixer")
                if fixer:
                    with self._agent_step(task, "content_fixer"):
                        task.final_content = fixer.fix(task, review_result.issues)
                    logger.info(f"任務 {task_id} 內容修正完成")
                else:
                    task.final_content = task.revised_content or task.optimized_content or task.draft_content or ""
            else:
                task.final_content = task.revised_content or task.optimized_content or task.draft_content or ""
            
            # 最終審核
            self.state.update_task_status(task_id, TaskStatus.REVIEWING)
            final_reviewer = self._get_agent("final_reviewer")
            if final_reviewer:
                with self._agent_step(task, "final_reviewer"):
                    task.final_content = final_reviewer.final_review(task.final_content, task)
                logger.info(f"任務 {task_id} 最終審核完成")
            
            # Frontend Pipeline with Retry Loop
            # Step: Frontend Generation (outside retry - generates once)
            self.state.update_task_status(task_id, TaskStatus.FRONTEND_GENERATING)
            frontend_agent = self._get_agent("frontend")
            if not frontend_agent:
                logger.error("FrontendAgent 未配置")
                self.state.update_task_status(task_id, TaskStatus.FAILED, error_message="FrontendAgent not available")
                return False
            
            frontend_request = FrontendRequest(
                task_id=task.id,
                content_type=task.content_type,
                frontend_scope="page",
                animation_required=False,
                design_brief=task.description or f"Create a frontend for: {task.title}",
                brand_constraints={},
                seo_title=task.seo_title or "",
                seo_description=task.seo_description or "",
                seo_keywords=task.seo_keywords or [],
                content_outline=[],
            )
            # Manually serialize to handle enum
            task.frontend_request = {
                "task_id": frontend_request.task_id,
                "content_type": frontend_request.content_type.name if hasattr(frontend_request.content_type, 'name') else str(frontend_request.content_type),
                "frontend_scope": frontend_request.frontend_scope,
                "animation_required": frontend_request.animation_required,
                "design_brief": frontend_request.design_brief,
                "brand_constraints": frontend_request.brand_constraints,
                "seo_title": frontend_request.seo_title,
                "seo_description": frontend_request.seo_description,
                "seo_keywords": frontend_request.seo_keywords,
                "content_outline": frontend_request.content_outline,
                "metadata": frontend_request.metadata,
            }
            
            with self._agent_step(task, "frontend"):
                frontend_result = frontend_agent.generate_frontend(frontend_request)
                task.frontend_result = asdict(frontend_result)
            logger.info(f"任務 {task_id} 前端生成完成: success={frontend_result.success}")
            
            if not frontend_result.success:
                self.state.update_task_status(task_id, TaskStatus.FAILED, error_message=f"Frontend generation failed: {frontend_result.errors}")
                return False
            
            # Frontend Pipeline Retry Loop: Security -> Conversion -> Validation
            frontend_pipeline_passed = False
            
            while task.frontend_retry_count < task.max_frontend_retries:
                attempt = task.frontend_retry_count + 1
                
                # Structured log: frontend_retry_started
                logger.info(
                    "frontend_retry_started",
                    extra={
                        "event": "frontend_retry_started",
                        "task_id": task_id,
                        "attempt": attempt,
                        "gate": "FRONTEND_PIPELINE",
                        "status": "started",
                        "error": None,
                    }
                )
                logger.info(f"任務 {task_id} 前端管線嘗試 {attempt}/{task.max_frontend_retries}")
                
                # Step: Frontend Security Gate
                self.state.update_task_status(task_id, TaskStatus.FRONTEND_SECURITY_CHECK)
                security_gate = FrontendSecurityGate(self.runtime_config)
                with self._agent_step(task, "frontend_security"):
                    security_result = security_gate.check(
                        html=frontend_result.html or "",
                        css=frontend_result.css or "",
                        javascript=frontend_result.javascript or "",
                    )
                    task.frontend_security_result = asdict(security_result)
                logger.info(f"任務 {task_id} 前端安全檢查完成: passed={security_result.passed}")
                
                if not security_result.passed:
                    feedback = self._create_frontend_failure_feedback(
                        gate=FrontendGate.FRONTEND_SECURITY,
                        result=security_result,
                        attempt=attempt,
                    )
                    self._record_frontend_retry(task, feedback)
                    task.frontend_retry_count += 1
                    
                    # Structured log: frontend_gate_failed
                    logger.warning(
                        "frontend_gate_failed",
                        extra={
                            "event": "frontend_gate_failed",
                            "task_id": task_id,
                            "attempt": attempt,
                            "gate": "FRONTEND_SECURITY",
                            "status": "failed",
                            "error": str(security_result.errors) if security_result.errors else "Security validation failed",
                        }
                    )
                    
                    logger.warning(f"任務 {task_id} 前端安全檢查失敗，重試次數: {task.frontend_retry_count}/{task.max_frontend_retries}")
                    if task.frontend_retry_count >= task.max_frontend_retries:
                        break
                    # Regenerate frontend with feedback
                    frontend_result = self._regenerate_frontend_with_feedback(frontend_agent, frontend_request, feedback)
                    if not frontend_result.success:
                        self.state.update_task_status(task_id, TaskStatus.FAILED, error_message=f"Frontend regeneration failed: {frontend_result.errors}")
                        return False
                    task.frontend_result = asdict(frontend_result)
                    continue
                
                # Step: GreenLight Conversion
                self.state.update_task_status(task_id, TaskStatus.FRONTEND_CONVERTING)
                converter = GreenLightConverter(self.runtime_config)
                with self._agent_step(task, "greenlight_conversion"):
                    conversion_result = converter.convert(security_result.html)
                    task.frontend_conversion_result = asdict(conversion_result)
                logger.info(f"任務 {task_id} GreenLight 轉換完成: success={conversion_result.success}")
                
                if not conversion_result.success:
                    feedback = self._create_frontend_failure_feedback(
                        gate=FrontendGate.FRONTEND_CONVERSION,
                        result=conversion_result,
                        attempt=attempt,
                    )
                    self._record_frontend_retry(task, feedback)
                    task.frontend_retry_count += 1
                    
                    # Structured log: frontend_gate_failed
                    logger.warning(
                        "frontend_gate_failed",
                        extra={
                            "event": "frontend_gate_failed",
                            "task_id": task_id,
                            "attempt": attempt,
                            "gate": "FRONTEND_CONVERSION",
                            "status": "failed",
                            "error": str(conversion_result.errors) if conversion_result.errors else "Conversion failed",
                        }
                    )
                    
                    logger.warning(f"任務 {task_id} GreenLight 轉換失敗，重試次數: {task.frontend_retry_count}/{task.max_frontend_retries}")
                    if task.frontend_retry_count >= task.max_frontend_retries:
                        break
                    # Regenerate frontend with feedback
                    frontend_result = self._regenerate_frontend_with_feedback(frontend_agent, frontend_request, feedback)
                    if not frontend_result.success:
                        self.state.update_task_status(task_id, TaskStatus.FAILED, error_message=f"Frontend regeneration failed: {frontend_result.errors}")
                        return False
                    task.frontend_result = asdict(frontend_result)
                    continue
                
                # Update frontend_result with converted blocks for validation
                frontend_result.blocks = conversion_result.blocks
                task.frontend_result = asdict(frontend_result)
                
                # Step: Frontend Validation
                self.state.update_task_status(task_id, TaskStatus.FRONTEND_VALIDATING)
                validator = FrontendValidator(self.runtime_config)
                with self._agent_step(task, "frontend_validation"):
                    validation_result = validator.validate(frontend_result)
                    task.frontend_validation_result = asdict(validation_result)
                logger.info(f"任務 {task_id} 前端驗證完成: passed={validation_result.passed}, status={validation_result.validation_status}")
                
                if not validation_result.passed:
                    feedback = self._create_frontend_failure_feedback(
                        gate=FrontendGate.FRONTEND_VALIDATION,
                        result=validation_result,
                        attempt=attempt,
                    )
                    self._record_frontend_retry(task, feedback)
                    task.frontend_retry_count += 1
                    
                    # Structured log: frontend_gate_failed
                    logger.warning(
                        "frontend_gate_failed",
                        extra={
                            "event": "frontend_gate_failed",
                            "task_id": task_id,
                            "attempt": attempt,
                            "gate": "FRONTEND_VALIDATION",
                            "status": "failed",
                            "error": str(validation_result.errors) if validation_result.errors else "Validation failed",
                        }
                    )
                    
                    logger.warning(f"任務 {task_id} 前端驗證失敗，重試次數: {task.frontend_retry_count}/{task.max_frontend_retries}")
                    if task.frontend_retry_count >= task.max_frontend_retries:
                        break
                    # Regenerate frontend with feedback
                    frontend_result = self._regenerate_frontend_with_feedback(frontend_agent, frontend_request, feedback)
                    if not frontend_result.success:
                        self.state.update_task_status(task_id, TaskStatus.FAILED, error_message=f"Frontend regeneration failed: {frontend_result.errors}")
                        return False
                    task.frontend_result = asdict(frontend_result)
                    continue
                
                # Step: Frontend Production Quality Gate
                self.state.update_task_status(task_id, TaskStatus.FRONTEND_PRODUCTION_QUALITY_CHECK)
                production_gate = FrontendProductionQualityGate(self.runtime_config)
                
                # Resolve brand rules from client profile
                brand_rules = None
                if task.client_profile and task.client_profile.get("brand_profile"):
                    brand_profile_data = task.client_profile["brand_profile"]
                    if brand_profile_data.get("production_rules"):
                        brand_rules = BrandProductionRules.from_dict(brand_profile_data["production_rules"])
                
                with self._agent_step(task, "production_quality"):
                    production_result = production_gate.check(
                        html=frontend_result.html or "",
                        css=frontend_result.css or "",
                        javascript=frontend_result.javascript or "",
                        frontend_scope=frontend_request.frontend_scope,
                        content_type=frontend_request.content_type.name if hasattr(frontend_request.content_type, 'name') else str(frontend_request.content_type),
                        brand_rules=brand_rules,
                    )
                    task.frontend_production_quality_result = asdict(production_result)
                logger.info(f"任務 {task_id} 前端生產品質檢查完成: passed={production_result.passed}, status={production_result.validation_status}")
                
                if not production_result.passed:
                    feedback = self._create_frontend_failure_feedback(
                        gate=FrontendGate.FRONTEND_PRODUCTION_QUALITY,
                        result=production_result,
                        attempt=attempt,
                    )
                    self._record_frontend_retry(task, feedback)
                    task.frontend_retry_count += 1
                    
                    # Structured log: frontend_gate_failed
                    logger.warning(
                        "frontend_gate_failed",
                        extra={
                            "event": "frontend_gate_failed",
                            "task_id": task_id,
                            "attempt": attempt,
                            "gate": "FRONTEND_PRODUCTION_QUALITY",
                            "status": "failed",
                            "error": str(production_result.errors) if production_result.errors else "Production quality check failed",
                        }
                    )
                    
                    logger.warning(f"任務 {task_id} 前端生產品質檢查失敗，重試次數: {task.frontend_retry_count}/{task.max_frontend_retries}")
                    if task.frontend_retry_count >= task.max_frontend_retries:
                        break
                    # Regenerate frontend with feedback
                    frontend_result = self._regenerate_frontend_with_feedback(frontend_agent, frontend_request, feedback)
                    if not frontend_result.success:
                        self.state.update_task_status(task_id, TaskStatus.FAILED, error_message=f"Frontend regeneration failed: {frontend_result.errors}")
                        return False
                    task.frontend_result = asdict(frontend_result)
                    continue
                
                # All gates passed
                frontend_pipeline_passed = True
                
                # Phase 7D-5D: Prepare image artifact BEFORE preview rendering
                self.state.update_task_status(task_id, TaskStatus.GENERATING_IMAGE)
                with self._agent_step(task, "image_preparation"):
                    image_artifact = self._prepare_image_artifact(task)
                
                # Phase 7D-5E: Deterministic image preparation guard
                # Explicit disabled check using existing config semantics
                image_enabled = getattr(self.runtime_config, "agents", {}).get("image", {}).get("enabled", True)

                if not image_enabled:
                    # Intentional absence - image generation explicitly disabled
                    logger.info(f"任務 {task.id} 圖片生成已禁用，繼續無圖片流程")
                    image_artifact = None
                elif image_artifact is None:
                    # Image agent enabled but no artifact produced - unsafe condition
                    logger.error(f"任務 {task.id} 圖片生成已啟用但未產出 artifact，失敗關閉")
                    task.final_failed_gate = "IMAGE_PREPARATION"
                    task.final_error = "Image generation enabled but no artifact produced"
                    task.final_feedback = "Image preparation failed: image agent enabled but returned no artifact"
                    task.final_failure_category = FailureCategory.IMAGE.value
                    task.failure_timestamp = datetime.datetime.now().isoformat()
                    self.state.update_task_status(
                        task_id,
                        TaskStatus.FAILED_NEEDS_ATTENTION,
                        error_message="Image preparation failed: no artifact produced"
                    )
                    return False
                elif image_artifact.status != ImageArtifactStatus.READY:
                    # FAILED, PENDING, or any non-READY status → hard stop
                    logger.error(f"任務 {task.id} 圖片 artifact 狀態非 READY: {image_artifact.status.value}，失敗關閉")
                    task.final_failed_gate = "IMAGE_PREPARATION"
                    task.final_error = f"Image artifact status: {image_artifact.status.value}"
                    if image_artifact.metadata and "error" in image_artifact.metadata:
                        task.final_error = f"{task.final_error}: {image_artifact.metadata['error']}"
                    task.final_feedback = f"Image preparation {image_artifact.status.value}. No hero image available for preview or publish."
                    task.final_failure_category = FailureCategory.IMAGE.value
                    task.failure_timestamp = datetime.datetime.now().isoformat()
                    self.state.update_task_status(
                        task_id,
                        TaskStatus.FAILED_NEEDS_ATTENTION,
                        error_message=f"Image preparation failed: {task.final_error}"
                    )
                    return False
                # READY → continue to PreviewRenderer

                # Phase 7D-1: Preview Rendering
                self.state.update_task_status(task_id, TaskStatus.FRONTEND_PREVIEW_RENDERING)
                preview_renderer = PreviewRenderer(self.runtime_config)
                
                # attempt_number = frontend_retry_count + 1 (current attempt)
                attempt_number = task.frontend_retry_count + 1
                
                with self._agent_step(task, "preview"):
                    preview_artifact, preview_failure, rendered_evidence = preview_renderer.render(
                        task_id=task_id,
                        frontend_result=frontend_result,
                        attempt_number=attempt_number,
                        image_artifact=image_artifact,
                    )
                
                if preview_failure:
                    # Infrastructure failure - do NOT increment frontend_retry_count
                    # Retry rendering same artifact up to MAX_INFRA_RETRIES (handled inside renderer)
                    # If we get here, retries are exhausted
                    
                    # Set final failure state for infrastructure failure
                    task.final_failed_gate = "PREVIEW_RENDER"
                    task.final_error = f"Preview infrastructure failure: {preview_failure.error_type}: {preview_failure.message}"
                    task.final_feedback = "Browser/render infrastructure failure. Frontend content passed all quality gates."
                    task.final_failure_category = (
                        preview_failure.failure_category.value
                        if isinstance(preview_failure.failure_category, FailureCategory)
                        else preview_failure.failure_category
                    )
                    task.failure_timestamp = datetime.datetime.now().isoformat()
                    
                    self.state.update_task_status(
                        task_id,
                        TaskStatus.FAILED_NEEDS_ATTENTION,
                        error_message=f"Preview rendering failed after retries: {preview_failure.message}"
                    )
                    
                    logger.error(
                        "preview_infrastructure_failure",
                        extra={
                            "event": "preview_infrastructure_failure",
                            "task_id": task_id,
                            "attempt": attempt_number,
                            "gate": "PREVIEW_RENDER",
                            "status": "failed",
                            "error": f"{preview_failure.error_type}: {preview_failure.message}",
                            "failure_category": "infrastructure",
                        }
                    )
                    
                    logger.error(f"任務 {task_id} 預覽渲染失敗 (基礎設施): {preview_failure.message}")
                    return False
                
                # Success: persist preview artifact
                task.preview_history.append(preview_artifact.to_dict())
                
                logger.info(
                    f"任務 {task_id} 預覽渲染完成: preview_id={preview_artifact.preview_id}, "
                    f"desktop_viewport={preview_artifact.desktop_viewport_screenshot_path}, "
                    f"mobile_viewport={preview_artifact.mobile_viewport_screenshot_path}"
                )
                
                # Phase 7D-2B: Rendered Technical Validation
                self.state.update_task_status(
                    task_id,
                    TaskStatus.FRONTEND_RENDERED_TECHNICAL_CHECK,
                )
                rendered_technical_validator = RenderedTechnicalValidator(self.runtime_config)
                with self._agent_step(task, "rendered_technical"):
                    rendered_technical_result = rendered_technical_validator.validate(
                        rendered_evidence
                    )
                    task.rendered_technical_result = rendered_technical_result.to_dict()
                task.rendered_technical_history.append(rendered_technical_result.to_dict())

                if rendered_technical_result.passed:
                    with self._agent_step(task, "visual_quality"):
                        visual_quality_result = self._run_visual_quality_review(task, preview_artifact)

                logger.info(

                    f"任務 {task_id} 渲染技術檢查完成: "
                    f"passed={rendered_technical_result.passed}, "
                    f"status={rendered_technical_result.validation_status}, "
                    f"errors={len(rendered_technical_result.errors)}, "
                    f"warnings={len(rendered_technical_result.warnings)}"
                )
                
                if not rendered_technical_result.passed:
                    feedback = self._create_frontend_failure_feedback(
                        gate=FrontendGate.RENDERED_TECHNICAL,
                        result=rendered_technical_result,
                        attempt=attempt_number,
                    )
                    self._record_frontend_retry(task, feedback)
                    task.frontend_retry_count += 1
                    frontend_pipeline_passed = False
                    
                    logger.warning(
                        "frontend_gate_failed",
                        extra={
                            "event": "frontend_gate_failed",
                            "task_id": task_id,
                            "attempt": attempt_number,
                            "gate": "RENDERED_TECHNICAL",
                            "status": "failed",
                            "error": str(rendered_technical_result.errors)
                            if rendered_technical_result.errors
                            else "Rendered technical validation failed",
                            "failure_category": FailureCategory.CONTENT.value,
                        }
                    )
                    
                    logger.warning(
                        f"任務 {task_id} 渲染技術檢查失敗，重試次數: "
                        f"{task.frontend_retry_count}/{task.max_frontend_retries}"
                    )
                    if task.frontend_retry_count >= task.max_frontend_retries:
                        break
                    
                    frontend_result = self._regenerate_frontend_with_feedback(
                        frontend_agent, frontend_request, feedback
                    )
                    if not frontend_result.success:
                        self.state.update_task_status(
                            task_id,
                            TaskStatus.FAILED,
                            error_message=f"Frontend regeneration failed: {frontend_result.errors}",
                        )
                        return False
                    task.frontend_result = asdict(frontend_result)
                    continue
                
                break

            # Handle visual review HUMAN_REVIEW escalation
            visual_action = None
            if task.visual_quality_result:
                visual_action = task.visual_quality_result.get("action")
            if task.status == TaskStatus.FRONTEND_VISUAL_REVIEW and visual_action == VisualQualityAction.HUMAN_REVIEW.value:
                approval_policy = self._resolve_approval_policy(task)
                task.approval_policy = approval_policy.to_dict()
                task.status = TaskStatus.AWAITING_APPROVAL
                task.approval_status = "pending"
                task.approval_requested_at = datetime.datetime.now().isoformat()
                task.updated_at = datetime.datetime.now().isoformat()
                logger.info(f"任務 {task_id} 視覺審查要求人工批准 (AWAITING_APPROVAL)")
                self.save_state()
                return False

            if not frontend_pipeline_passed:

                # Retry exhausted - set final failure state
                self._finalize_frontend_failure(task)
                
                # Structured log: frontend_retry_exhausted
                logger.error(
                    "frontend_retry_exhausted",
                    extra={
                        "event": "frontend_retry_exhausted",
                        "task_id": task_id,
                        "attempt": task.frontend_retry_count,
                        "gate": task.final_failed_gate or "UNKNOWN",
                        "status": "exhausted",
                        "error": task.final_error or "Frontend pipeline failed after max retries",
                    }
                )
                
                logger.error(f"任務 {task_id} 前端管線重試耗盡，狀態設為 FAILED_NEEDS_ATTENTION")
                return False
            
            # Use converted blocks as the content for publishing
            task.final_content = conversion_result.blocks
            
            # Phase 7C-4: Resolve ApprovalPolicy
            approval_policy = self._resolve_approval_policy(task)
            task.approval_policy = approval_policy.to_dict()
            logger.info(f"任務 {task_id} 核准策略: {approval_policy.mode.value}")
            
            if approval_policy.mode == ApprovalPolicyMode.AUTO_PUBLISH:
                # AUTO_PUBLISH: continue directly to ImageAgent -> Publisher
                logger.info(f"任務 {task_id} 自動發布模式，繼續工作流程")
                return self._continue_post_approval(task_id)
            else:
                # REQUIRE_HUMAN_REVIEW: enter AWAITING_APPROVAL
                task.status = TaskStatus.AWAITING_APPROVAL
                task.approval_status = "pending"
                task.approval_requested_at = datetime.datetime.now().isoformat()
                task.updated_at = datetime.datetime.now().isoformat()
                logger.info(f"任務 {task_id} 等待人工批准發布 (AWAITING_APPROVAL)")
                
                # Save state for human to review
                self.save_state()
                return False  # Workflow paused, not failed
            
        except ObserverError:
            raise
        except Exception as e:
            safe_error = self._workflow_error(e)
            logger.error(f"任務 {task_id} 失敗: {safe_error}")
            self.state.update_task_status(
                task_id, 
                TaskStatus.FAILED, 
                error_message=safe_error
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
        from agents.frontend import FrontendAgent
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
            "frontend": FrontendAgent,
            "publisher": WordPressPublisher,
        }
        
        agent_class = agents.get(agent_type)
        if not agent_class:
            return None
        
        enabled = getattr(self.runtime_config, "agents", {}).get(agent_type, {}).get("enabled", True)
        if not enabled:
            return None
        
        if agent_type == "publisher":
            return agent_class(self.runtime_config)
        settings = agent_settings(self.runtime_config)
        if agent_type == "image":
            return agent_class(settings,providers=self.providers,
                               upload_media=legacy_media_upload(self.runtime_config))
        return agent_class(settings,providers=self.providers)

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
        
        with self._agent_step(task, "writer"):
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
        
        with self._agent_step(task, "writer"):
            rewritten = writer.call_ai(prompt, temperature=0.7, max_tokens=4000)
        return rewritten.strip()

    def _create_frontend_failure_feedback(
        self,
        gate: FrontendGate,
        result: Any,
        attempt: int,
    ) -> FrontendFailureFeedback:
        """Create structured failure feedback for a frontend pipeline gate failure.
        
        Args:
            gate: The frontend gate that failed.
            result: The result object from the failed gate (SecurityResult, ConversionResult, or ValidationResult).
            attempt: The current attempt number.
            
        Returns:
            FrontendFailureFeedback: Structured feedback for the failure.
        """
        # Extract error message and details from result
        errors = getattr(result, 'errors', []) or []
        warnings = getattr(result, 'warnings', []) or []
        blocked_items = getattr(result, 'blocked_items', []) or []
        
        # Handle errors that might be dicts (validation) or strings (security/conversion)
        error_parts = []
        for e in errors:
            if isinstance(e, dict):
                error_parts.append(f"{e.get('type', 'unknown')}: {e.get('message', '')}")
            else:
                error_parts.append(str(e))
        error_msg = "; ".join(error_parts) if error_parts else "Unknown error"
        
        warning_parts = []
        for w in warnings:
            if isinstance(w, dict):
                warning_parts.append(f"{w.get('type', 'unknown')}: {w.get('message', '')}")
            else:
                warning_parts.append(str(w))
        warning_msg = "; ".join(warning_parts) if warning_parts else ""
        
        # Build actionable feedback based on gate
        if gate == FrontendGate.FRONTEND_SECURITY:
            feedback = (
                f"Security gate rejected the frontend output. "
                f"Errors: {error_msg}. "
                f"Blocked items: {', '.join(blocked_items) if blocked_items else 'none'}. "
                f"Please regenerate HTML/CSS/JS without dangerous tags, event handlers, "
                f"javascript: URLs, eval(), external resources from non-allowlisted domains, "
                f"or CSS expressions. Ensure all sizes are within limits."
            )
            details = {
                "blocked_items": blocked_items,
                "warnings": warnings,
                "html_size": len(getattr(result, 'html', '') or ''),
                "css_size": len(getattr(result, 'css', '') or ''),
                "js_size": len(getattr(result, 'javascript', '') or ''),
            }
        elif gate == FrontendGate.FRONTEND_CONVERSION:
            feedback = (
                f"GreenLight conversion failed. "
                f"Errors: {error_msg}. "
                f"Warnings: {warning_msg}. "
                f"Please ensure the HTML structure is compatible with Greenshift blocks. "
                f"Check for unsupported HTML tags, malformed markup, or converter script issues."
            )
            details = {
                "warnings": warnings,
                "input_html_preview": (getattr(result, 'html', '') or '')[:500] if hasattr(result, 'html') else "",
            }
        elif gate == FrontendGate.RENDERED_TECHNICAL:
            error_messages = []
            for error in errors:
                error_type = error.get("type", "rendered_technical_error")
                context = error.get("context", {})
                viewport = context.get("viewport", "unknown")
                if error_type == "document_horizontal_overflow":
                    scroll_width = context.get("scroll_width", 0)
                    client_width = context.get("client_width", 0)
                    overflow_px = context.get("overflow_px", scroll_width - client_width)
                    message = (
                        f"{viewport} viewport document width {scroll_width}px exceeds "
                        f"client width {client_width}px by {overflow_px}px. Inspect "
                        f"fixed-width elements and responsive container sizing."
                    )
                elif error_type == "broken_image":
                    src = context.get("src", "<unknown>")
                    complete = context.get("complete", False)
                    natural_width = context.get("natural_width", 0)
                    natural_height = context.get("natural_height")
                    height_part = (
                        f", naturalHeight={natural_height}" if natural_height is not None else ""
                    )
                    message = (
                        f"Broken image detected in {viewport} viewport: src={src}, "
                        f"complete={complete}, naturalWidth={natural_width}{height_part}."
                    )
                elif error_type == "page_runtime_error":
                    runtime_message = context.get("message", error.get("message", "unknown error"))
                    location = context.get("location", error.get("location", {}))
                    location_part = ""
                    if location:
                        location_part = (
                            f" at {location.get('url', '')}:"
                            f"{location.get('line', 0)}:{location.get('column', 0)}"
                        )
                    message = f"Page runtime error in {viewport} preview: {runtime_message}{location_part}."
                else:
                    message = error.get("message", error_type)
                error_messages.append(f"{error_type}: {message}")
            
            feedback = (
                "Rendered technical validation failed. Fix the browser-rendered defects "
                "and regenerate the frontend: " + "; ".join(error_messages) + " "
                "Re-run the full frontend pipeline after fixing these issues."
            )
            details = {
                "errors": errors,
                "diagnostics": getattr(result, "diagnostics", {}),
                "failure_category": FailureCategory.CONTENT.value,
            }
        elif gate == FrontendGate.FRONTEND_PRODUCTION_QUALITY:
            feedback = (
                f"Frontend production quality check failed. "
                f"Errors: {error_msg}. "
                f"Warnings: {warning_msg}. "
                f"Please regenerate HTML/CSS/JS to meet production quality standards: "
                f"reduce external dependencies, inline code size, !important usage, "
                f"animation budget, fix accessibility issues (missing alt, form labels, heading hierarchy), "
                f"and responsive safety (fixed widths, media queries)."
            )
            details = {
                "errors": errors,
                "warnings": warnings,
                "diagnostics": getattr(result, 'diagnostics', {}),
            }
        else:  # FRONTEND_VALIDATION
            feedback = (
                f"Frontend validation failed. "
                f"Errors: {error_msg}. "
                f"Please fix HTML structure (unclosed tags), CSS syntax (unmatched braces), "
                f"JavaScript syntax (parseable by Node.js), and WordPress block format "
                f"(balanced wp: comments with valid JSON attributes)."
            )
            details = {
                "validation_errors": errors,
                "validation_warnings": warnings,
                "diagnostics": getattr(result, 'diagnostics', {}),
            }
        
        severity = FrontendFailureSeverity.ERROR
        if gate == FrontendGate.FRONTEND_VALIDATION and not error_parts:
            severity = FrontendFailureSeverity.WARNING
        
        return FrontendFailureFeedback(
            gate=gate,
            severity=severity,
            error=error_msg,
            feedback=feedback,
            details=details,
        )

    def _record_frontend_retry(self, task: Task, feedback: FrontendFailureFeedback) -> None:
        """Record a frontend retry attempt in the task's retry history.
        
        Args:
            task: The task being processed.
            feedback: The failure feedback to record.
        """
        history_entry = {
            "attempt": task.frontend_retry_count + 1,
            "gate": feedback.gate.value if isinstance(feedback.gate, FrontendGate) else feedback.gate,
            "error": feedback.error,
            "feedback": feedback.feedback,
            "action": "FRONTEND_REGENERATE",
            "timestamp": feedback.timestamp,
            "details": feedback.details,
            "failure_category": feedback.details.get(
                "failure_category", FailureCategory.CONTENT.value
            ),
        }
        task.frontend_retry_history.append(history_entry)
        
        # Structured logging
        logger.info(
            f"Frontend retry recorded: task_id={task.id}, "
            f"attempt={history_entry['attempt']}, gate={history_entry['gate']}, "
            f"error={feedback.error[:100]}..."
        )

    def _regenerate_frontend_with_feedback(
        self,
        frontend_agent: FrontendAgent,
        request: FrontendRequest,
        feedback: FrontendFailureFeedback,
    ) -> FrontendResult:
        """Regenerate frontend with failure feedback injected into the prompt.
        
        Args:
            frontend_agent: The FrontendAgent instance.
            request: The original FrontendRequest.
            feedback: The failure feedback to incorporate.
            
        Returns:
            FrontendResult: The new frontend generation result.
        """
        # Build feedback-enhanced prompt
        feedback_prompt = (
            f"\n\nIMPORTANT: The previous attempt failed at the {feedback.gate.value} gate.\n"
            f"Error: {feedback.error}\n"
            f"Required fix: {feedback.feedback}\n"
            f"Please generate corrected HTML, CSS, and JavaScript that addresses these issues."
        )
        
        # Create a modified request with feedback in design_brief
        enhanced_request = FrontendRequest(
            task_id=request.task_id,
            content_type=request.content_type,
            frontend_scope=request.frontend_scope,
            animation_required=request.animation_required,
            design_brief=request.design_brief + feedback_prompt,
            brand_constraints=request.brand_constraints,
            seo_title=request.seo_title,
            seo_description=request.seo_description,
            seo_keywords=request.seo_keywords,
            content_outline=request.content_outline,
            metadata=request.metadata,
        )
        
        task = self.state.get_task(request.task_id)
        if task is None:
            return frontend_agent.generate_frontend(enhanced_request)
        with self._agent_step(task, "frontend_retry"):
            result = frontend_agent.generate_frontend(enhanced_request)
        return result

    def _finalize_frontend_failure(self, task: Task) -> None:
        """Finalize task state after frontend retry exhaustion.
        
        Args:
            task: The task that failed all frontend retries.
        """
        import datetime
        
        # Determine the final failed gate from history
        final_gate = None
        final_error = None
        final_feedback = None
        final_failure_category = FailureCategory.CONTENT.value
        
        if task.frontend_retry_history:
            last_entry = task.frontend_retry_history[-1]
            final_gate = last_entry.get("gate")
            final_error = last_entry.get("error")
            final_feedback = last_entry.get("feedback")
            final_failure_category = last_entry.get(
                "failure_category", FailureCategory.CONTENT.value
            )
        
        task.final_failed_gate = final_gate
        task.final_error = final_error
        task.final_feedback = final_feedback
        task.final_failure_category = final_failure_category
        task.failure_timestamp = datetime.datetime.now().isoformat()
        
        self.state.update_task_status(
            task.id,
            TaskStatus.FAILED_NEEDS_ATTENTION,
            error_message=f"Frontend pipeline failed after {task.max_frontend_retries} retries. "
                          f"Last failure at {final_gate}: {final_error}"
        )
        
        logger.error(
            f"Task {task.id} entered FAILED_NEEDS_ATTENTION: "
            f"frontend_retry_count={task.frontend_retry_count}, "
            f"final_gate={final_gate}, final_error={final_error}"
        )
        
        # Structured log: task_entered_FAILED_NEEDS_ATTENTION
        logger.error(
            "task_entered_FAILED_NEEDS_ATTENTION",
            extra={
                "event": "task_entered_FAILED_NEEDS_ATTENTION",
                "task_id": task.id,
                "attempt": task.frontend_retry_count,
                "gate": final_gate or "UNKNOWN",
                "status": "FAILED_NEEDS_ATTENTION",
                "error": final_error or "Frontend pipeline failed after max retries",
            }
        )

    def _run_visual_quality_review(
        self,
        task: Task,
        preview_artifact: PreviewArtifact,
    ) -> VisualQualityResult:
        self.state.update_task_status(task.id, TaskStatus.FRONTEND_VISUAL_REVIEW)
        try:
            reviewer = VisualQualityReviewer(agent_settings(self.runtime_config),providers=self.providers)
            result = reviewer.review(preview_artifact)
            result_data = result.to_dict()
        except ObserverError:
            raise
        except Exception as exc:
            result = VisualQualityResult(
                action=VisualQualityAction.HUMAN_REVIEW,
                summary=f"Visual review failed: {classify_error(exc).safe_summary}",
                issues=[],
                reviewed_viewports=[],
                reviewer="visual_quality_reviewer",
            )
            result_data = result.to_dict()

        task.visual_quality_result = result_data
        task.visual_quality_history.append(result_data)
        logger.info(
            f"任務 {task.id} 視覺品質審查完成: "
            f"action={result.action.value if isinstance(result.action, VisualQualityAction) else result.action}"
        )
        return result

    def _prepare_image_artifact(self, task: Task) -> Optional[ImageArtifact]:
        """Prepare or reuse an ImageArtifact for the task.
        
        Implements idempotent reuse:
        - If Task.image_artifact exists with status READY → return it (no ImageAgent call)
        - If Task.image_artifact exists with status FAILED/PENDING → return it (no regeneration)
        - If no image_artifact → call existing ImageAgent, materialize result into ImageArtifact
        
        Args:
            task: The task to prepare image artifact for.
            
        Returns:
            ImageArtifact if available/created, None if image generation disabled.
        """
        # Check if image agent is enabled
        if not getattr(self.runtime_config, "agents", {}).get("image", {}).get("enabled", True):
            logger.info(f"任務 {task.id} 圖片生成已禁用，跳過")
            return None
            
        # Reuse existing artifact (handles malformed persisted artifact)
        if task.image_artifact is not None:
            try:
                existing_artifact = ImageArtifact.from_dict(task.image_artifact)
            except ObserverError:
                raise
            except Exception as e:
                # Malformed persisted artifact → treat as FAILED
                logger.warning(f"任務 {task.id} 現有圖片 artifact 格式錯誤: {e}")
                failed_artifact = create_image_artifact(
                    status=ImageArtifactStatus.FAILED,
                    metadata={"error": f"Malformed persisted ImageArtifact: {e}"}
                )
                task.image_artifact = failed_artifact.to_dict()
                self.save_state()
                return failed_artifact

            if existing_artifact.status == ImageArtifactStatus.READY:
                if not self._is_usable_ready_image_artifact(existing_artifact):
                    error = (
                        "Malformed persisted ImageArtifact: READY artifact requires "
                        "a WordPress media ID and a preview-resolvable image source"
                    )
                    logger.warning(f"任務 {task.id} 現有 READY 圖片 artifact 無法使用")
                    failed_artifact = create_image_artifact(
                        artifact_id=existing_artifact.artifact_id,
                        status=ImageArtifactStatus.FAILED,
                        metadata={"error": error},
                    )
                    task.image_artifact = failed_artifact.to_dict()
                    self.save_state()
                    return failed_artifact
                logger.info(f"任務 {task.id} 重用現有 READY 圖片 artifact: {existing_artifact.artifact_id}")
                return existing_artifact
            elif existing_artifact.status == ImageArtifactStatus.FAILED:
                logger.info(f"任務 {task.id} 現有圖片 artifact 狀態為 FAILED，不再重試: {existing_artifact.artifact_id}")
                return existing_artifact
            elif existing_artifact.status == ImageArtifactStatus.PENDING:
                logger.info(f"任務 {task.id} 現有圖片 artifact 狀態為 PENDING，保持現狀: {existing_artifact.artifact_id}")
                return existing_artifact
        
        # No existing artifact - invoke existing ImageAgent
        logger.info(f"任務 {task.id} 開始準備圖片 artifact")
        image_agent = self._get_agent("image")
        if not image_agent:
            logger.warning(f"任務 {task.id} 無法獲取 ImageAgent")
            return None
            
        # Call existing ImageAgent behavior
        try:
            media_id, media_url = image_agent.generate_hero_image(task)
        except ObserverError:
            raise
        except Exception as e:
            logger.warning(f"任務 {task.id} 圖片生成異常: {str(e)}")
            # Create FAILED artifact
            failed_artifact = create_image_artifact(
                status=ImageArtifactStatus.FAILED,
                prompt=getattr(task, 'image_prompt', None),
                source_url=getattr(task, 'image_url', None),
                metadata={"error": str(e)}
            )
            task.image_artifact = failed_artifact.to_dict()
            self.save_state()
            return failed_artifact
        
        # Materialize result into ImageArtifact
        if media_id and media_url:
            # Success - create READY artifact
            artifact = create_image_artifact(
                status=ImageArtifactStatus.READY,
                wordpress_media_id=media_id,
                wordpress_media_url=media_url,
                source_url=getattr(task, 'image_url', None),
                prompt=getattr(task, 'image_prompt', None),
                provider="openai",  # From existing ImageAgent
                model="dall-e-3",   # From existing ImageAgent
                metadata={"generation": "success"}
            )
            # Sync legacy fields for backward compatibility
            task.hero_image_id = media_id
            task.hero_image_url = media_url
            task.image_status = "success"
        else:
            # Failure - create FAILED artifact (includes WordPress upload failure with source_url)
            artifact = create_image_artifact(
                status=ImageArtifactStatus.FAILED,
                prompt=getattr(task, 'image_prompt', None),
                source_url=getattr(task, 'image_url', None),
                metadata={"generation": "failed"}
            )
            # Sync legacy fields for backward compatibility
            task.image_status = "failed"
        
        # Persist artifact on Task
        task.image_artifact = artifact.to_dict()
        self.save_state()
        
        if artifact.status == ImageArtifactStatus.READY:
            logger.info(f"任務 {task.id} 圖片 artifact 創建完成: {artifact.artifact_id}")
        else:
            logger.warning(f"任務 {task.id} 圖片 artifact 創建失敗: {artifact.artifact_id}")
            
        return artifact

    @staticmethod
    def _is_usable_ready_image_artifact(artifact: ImageArtifact) -> bool:
        """Return whether a persisted READY artifact is safe to preview and publish."""
        if artifact.status != ImageArtifactStatus.READY or not artifact.wordpress_media_id:
            return False
        return bool(
            artifact.wordpress_media_url
            or artifact.source_url
            or (
                artifact.local_path
                and Path(artifact.local_path).expanduser().is_file()
            )
        )

    def _fail_approval_resume_image(self, task: Task, error: str) -> bool:
        """Fail approval resume at the image boundary without rerunning production stages."""
        task.image_status = "failed"
        task.final_failed_gate = "IMAGE_PREPARATION"
        task.final_error = error
        task.final_feedback = f"Image preparation failed during approval resume: {error}"
        task.final_failure_category = FailureCategory.IMAGE.value
        task.failure_timestamp = datetime.datetime.now().isoformat()
        self.state.update_task_status(
            task.id,
            TaskStatus.FAILED_NEEDS_ATTENTION,
            error_message=f"Image preparation failed: {error}",
        )
        self.save_state()
        return False

    def _resolve_approval_policy(self, task: Task) -> ApprovalPolicy:
        """Resolve the approval policy for a task.
        
        Priority:
        1. Task-level approval_policy (explicit)
        2. Task-level client_profile with approval_policy
        3. Default: REQUIRE_HUMAN_REVIEW
        
        Args:
            task: The task to resolve policy for.
            
        Returns:
            ApprovalPolicy: The resolved approval policy.
        """
        # Check task-level explicit approval policy
        if task.approval_policy:
            return ApprovalPolicy.from_dict(task.approval_policy)
        
        # Check client profile for approval policy
        if task.client_profile and task.client_profile.get("approval_policy"):
            return ApprovalPolicy.from_dict(task.client_profile["approval_policy"])
        
        # Default: REQUIRE_HUMAN_REVIEW
        return ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)

    def submit_approval_decision(
        self,
        task_id: str,
        approved: bool,
        feedback: Optional[str] = None
    ) -> bool:
        """Submit human approval decision for a task awaiting approval.
        
        Args:
            task_id: The task ID.
            approved: True if approved, False if rejected.
            feedback: Optional human feedback (required for rejection).
            
        Returns:
            bool: True if decision was processed successfully.
        """
        import datetime
        
        task = self.state.get_task(task_id)
        if not task:
            logger.error(f"Task not found: {task_id}")
            return False
        
        if task.status != TaskStatus.AWAITING_APPROVAL:
            logger.error(f"Task {task_id} is not awaiting approval (status: {task.status.name})")
            return False
        
        # Record decision
        task.approval_decision = approved
        task.approval_feedback = feedback
        task.approval_decided_at = datetime.datetime.now().isoformat()
        task.approval_status = "approved" if approved else "rejected"
        task.updated_at = datetime.datetime.now().isoformat()
        
        if approved:
            # Approved: resume workflow from post-approval point
            task.status = TaskStatus.GENERATING_IMAGE
            logger.info(f"Task {task_id} approved, resuming workflow")
            
            # Continue with image generation and publishing
            return self._continue_post_approval(task_id)
        else:
            # Rejected: move to REJECTED_NEEDS_REVISION
            if not feedback:
                logger.warning(f"Task {task_id} rejected without feedback")
            
            task.status = TaskStatus.REJECTED_NEEDS_REVISION
            logger.info(f"Task {task_id} rejected, status set to REJECTED_NEEDS_REVISION")
            
            # Save state
            self.save_state()
            return True

    def _continue_post_approval(self, task_id: str) -> bool:
        """Continue workflow after approval by validating the reviewed artifact, then publishing.
        
        Args:
            task_id: The task ID.
            
        Returns:
            bool: True if workflow completed successfully.
        """
        task = self.state.get_task(task_id)
        if not task:
            logger.error(f"Task not found: {task_id}")
            return False
        
        try:
            # Step 8: 圖片生成階段 - image artifact should already be prepared before preview
            # Use existing image artifact if available
            if task.image_artifact is not None:
                try:
                    persisted_artifact = ImageArtifact.from_dict(task.image_artifact)
                except ObserverError:
                    raise
                except Exception as e:
                    error = f"Malformed persisted ImageArtifact: {e}"
                    task.image_artifact = create_image_artifact(
                        status=ImageArtifactStatus.FAILED,
                        metadata={"error": error},
                    ).to_dict()
                    return self._fail_approval_resume_image(task, error)

                if not self._is_usable_ready_image_artifact(persisted_artifact):
                    if persisted_artifact.status == ImageArtifactStatus.READY:
                        error = (
                            "Malformed persisted ImageArtifact: READY artifact requires "
                            "a WordPress media ID and a preview-resolvable image source"
                        )
                        task.image_artifact = create_image_artifact(
                            artifact_id=persisted_artifact.artifact_id,
                            status=ImageArtifactStatus.FAILED,
                            metadata={"error": error},
                        ).to_dict()
                    else:
                        error = f"Image artifact status: {persisted_artifact.status.value}"
                        if persisted_artifact.metadata and "error" in persisted_artifact.metadata:
                            error = f"{error}: {persisted_artifact.metadata['error']}"
                    return self._fail_approval_resume_image(task, error)

            if task.image_artifact:
                artifact = ImageArtifact.from_dict(task.image_artifact)
                # Sync legacy fields for backward compatibility (should already be set)
                if artifact.status == ImageArtifactStatus.READY:
                    task.hero_image_id = artifact.wordpress_media_id
                    task.hero_image_url = artifact.wordpress_media_url
                    task.image_status = "success"
                    logger.info(f"任務 {task_id} 重用現有 READY 圖片 artifact: {artifact.artifact_id}")
                elif artifact.status == ImageArtifactStatus.FAILED:
                    task.image_status = "failed"
                    logger.warning(f"任務 {task_id} 現有圖片 artifact 狀態為 FAILED: {artifact.artifact_id}")
                else:  # PENDING
                    task.image_status = "failed"
                    logger.warning(f"任務 {task_id} 現有圖片 artifact 狀態為 PENDING，視為失敗: {artifact.artifact_id}")
            elif task.hero_image_id:
                # Legacy task with existing hero_image_id but no image_artifact
                logger.info(f"任務 {task_id} 使用舊版 hero_image_id: {task.hero_image_id}")
                task.image_status = "success"
            else:
                logger.info(f"任務 {task_id} 無圖片 artifact 或 hero_image_id，跳過圖片發布")
                task.image_status = "failed"
            
            # Step 9: 發布階段
            self.state.update_task_status(task_id, TaskStatus.PUBLISHING)
            publisher = self._get_agent("publisher")
            if publisher:
                featured_media_id = task.hero_image_id if task.image_status == "success" else None
                with self._agent_step(task, "publisher"):
                    wordpress_id, wordpress_url = publisher.publish_content(task, featured_media_id=featured_media_id)
                    task.wordpress_id = wordpress_id
                    task.wordpress_url = wordpress_url
                logger.info(f"任務 {task_id} 發布完成: {wordpress_url}")
            
            # Step 10: 完成
            self.state.update_task_status(task_id, TaskStatus.COMPLETED)
            logger.info(f"任務 {task_id} 已完成")
            
            # Step 11: 學習更新（發布後執行）
            self.state.update_task_status(task_id, TaskStatus.LEARNING)
            learner = self._get_agent("learner")
            if learner:
                with self._agent_step(task, "learner"):
                    learning_result = learner.analyze_review(task)
                proposals = learning_result.get("學習提案", [])
                if proposals:
                    task.learning_proposals.extend(proposals)
                    logger.info(f"任務 {task_id} 學習完成：產生 {len(proposals)} 個學習提案")
                else:
                    logger.info(f"任務 {task_id} 學習完成：無需更新規則庫")
            
            self.save_state()
            return True
            
        except ObserverError:
            raise
        except Exception as e:
            logger.error(f"任務 {task_id} 發布後流程失敗: {str(e)}")
            self.state.update_task_status(
                task_id, 
                TaskStatus.FAILED, 
                error_message=str(e)
            )
            return False

    def _manual_review_checkpoint(self, task: Task) -> bool:
        """人工校稿檢查點。暫停工作流程，等待人類審閱和修正。

        Args:
            task: 任務對象。

        Returns:
            bool: True 表示通過審核繼續執行，False 表示需要人工介入（非互動模式）或審核失敗。
        """
        content = task.final_content or task.revised_content or task.optimized_content or task.draft_content or ""

        # 非互動模式檢測：無法獲取人工審核，直接失敗關閉
        if not sys.stdin.isatty():
            logger.error(
                "非互動模式下無法進行人工校稿，任務停止於 MANUAL_REVIEW 階段。"
                " 請在互動式終端運行，或配置自動發布策略（未來功能）。"
            )
            self.state.update_task_status(
                task.id,
                TaskStatus.FAILED_NEEDS_ATTENTION,
                error_message="非互動模式：無人工審核可用，任務停止於 MANUAL_REVIEW"
            )
            return False

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
            logger.error(
                "互動模式下發生 EOF，無法獲取人工審核輸入，任務停止於 MANUAL_REVIEW 階段。"
            )
            self.state.update_task_status(
                task.id,
                TaskStatus.FAILED_NEEDS_ATTENTION,
                error_message="互動模式下發生 EOF：無人工審核輸入可用，任務停止於 MANUAL_REVIEW"
            )
            return False

        if user_input == ".":
            logger.info("使用者接受原稿")
            return True

        if user_input.lower() == "skip":
            logger.warning("使用者跳過校稿")
            return True

        if user_input:
            task.final_content = user_input
            logger.info("使用者提供了修正內容")
            return True

        return True

    def save_state(self, file_path: Optional[str] = None) -> None:
        """保存工作流程狀態到文件。
        
        Args:
            file_path: 保存文件的路徑（默認為 "workflow_state.json"）。
        """
        if file_path is None:
            if self._isolated:
                if self._observing_task is not None:
                    self._emit("checkpoint_produced", self._observing_task)
                return
            file_path = "workflow_state.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.state.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"工作流程狀態已保存到 {file_path}")

    def load_state(self, file_path: Optional[str] = None) -> None:
        """從文件加載工作流程狀態。
        
        Args:
            file_path: 文件路徑（默認為 "workflow_state.json"）。
        """
        if file_path is None:
            if self._isolated:
                raise ValueError("Isolated runs require an explicit state file")
            file_path = "workflow_state.json"
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Update existing workflow_state in place to preserve references
            loaded_state = WorkflowState.from_dict(data)
            self.state.tasks = loaded_state.tasks
            self.state.current_task_id = loaded_state.current_task_id
            self.state.global_state = loaded_state.global_state
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
