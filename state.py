# AI WordPress Factory 工作白板定義
# 存儲任務狀態、數據結構和工作流程的中間數據

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from enum import Enum, auto
import datetime


class TaskStatus(Enum):
    """任務狀態枚舉。"""
    PENDING = auto()       # 待處理
    PLANNING = auto()      # 規劃中
    RESEARCHING = auto()   # 調研中
    WRITING = auto()       # 撰寫中
    CRITIQUING = auto()    # 自我批評中
    REWRITING = auto()     # 修改中
    OPTIMIZING = auto()    # SEO 優化中
    REVIEWING = auto()     # 審閱中
    ROUTING = auto()       # 路由決策中
    MANUAL_REVIEW = auto() # 人工校稿中
    LEARNING = auto()      # 學習更新中
    GENERATING_IMAGE = auto()  # 圖片生成中
    # Frontend pipeline statuses
    FRONTEND_GENERATING = auto()  # 前端生成中
    FRONTEND_SECURITY_CHECK = auto()  # 前端安全檢查中
    FRONTEND_CONVERTING = auto()  # 前端轉換中
    FRONTEND_VALIDATING = auto()  # 前端驗證中
    FRONTEND_PRODUCTION_QUALITY_CHECK = auto()  # 前端生產品質檢查中
    FRONTEND_PREVIEW_RENDERING = auto()  # 前端預覽渲染中
    FRONTEND_RENDERED_TECHNICAL_CHECK = auto()  # 前端渲染技術檢查中
    FRONTEND_VISUAL_REVIEW = auto()  # 前端視覺品質審查中
    # Phase 7C-4: Approval workflow statuses
    AWAITING_APPROVAL = auto()  # 等待人工批准發布
    REJECTED_NEEDS_REVISION = auto()  # 人工拒絕，需要修訂
    PUBLISHING = auto()    # 發布中
    COMPLETED = auto()     # 已完成
    FAILED = auto()        # 失敗
    FAILED_NEEDS_ATTENTION = auto()  # 需要人工介入的失敗狀態


class ContentType(Enum):
    """內容類型枚舉。"""
    BLOG_POST = auto()     # 博客文章
    PAGE = auto()          # 頁面
    PRODUCT = auto()       # 產品


@dataclass
class Task:
    """單個任務的數據結構。"""
    id: str
    title: str
    description: Optional[str] = None
    content_type: ContentType = ContentType.BLOG_POST
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    
    # 工作流程相關數據
    plan: Optional[Dict[str, Any]] = None
    research_data: Optional[List[Dict[str, Any]]] = None
    draft_content: Optional[str] = None
    critique_result: Optional[Dict[str, Any]] = None
    quality_result: Optional[Dict[str, Any]] = None
    revised_content: Optional[str] = None
    optimized_content: Optional[str] = None
    final_content: Optional[str] = None
    
    # Content retries after the mandatory initial quality evaluation; excludes the initial attempt.
    retry_count: int = 0
    max_retries: int = 3
    
    # Frontend retry 保護
    frontend_retry_count: int = 0
    max_frontend_retries: int = 3
    frontend_retry_history: List[Dict[str, Any]] = field(default_factory=list)
    
    # Final frontend failure tracking
    final_failed_gate: Optional[str] = None
    final_error: Optional[str] = None
    final_feedback: Optional[str] = None
    final_failure_category: Optional[str] = None
    failure_timestamp: Optional[str] = None
    
    # 圖片相關
    image_prompt: Optional[str] = None
    image_url: Optional[str] = None
    hero_image_id: Optional[int] = None
    hero_image_url: Optional[str] = None
    image_status: Optional[str] = None
    
    # SEO 相關
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[List[str]] = None
    
    # 學習相關
    learning_proposals: List[Dict[str, Any]] = field(default_factory=list)
    
    # Frontend pipeline fields
    frontend_request: Optional[Dict[str, Any]] = None
    frontend_result: Optional[Dict[str, Any]] = None
    frontend_security_result: Optional[Dict[str, Any]] = None
    frontend_conversion_result: Optional[Dict[str, Any]] = None
    frontend_validation_result: Optional[Dict[str, Any]] = None
    frontend_production_quality_result: Optional[Dict[str, Any]] = None
    
# 發布相關
    wordpress_id: Optional[int] = None
    wordpress_url: Optional[str] = None
    
    # Phase 7C-4: Approval workflow fields
    client_profile: Optional[Dict[str, Any]] = None
    approval_policy: Optional[Dict[str, Any]] = None
    approval_status: Optional[str] = None  # "pending", "approved", "rejected"
    approval_requested_at: Optional[str] = None
    approval_decided_at: Optional[str] = None
    approval_feedback: Optional[str] = None
    approval_decision: Optional[bool] = None
    
    # Phase 7D-1: Preview rendering fields
    preview_history: List[Dict[str, Any]] = field(default_factory=list)
    # latest_preview is derived from preview_history[-1] for backward compat
    # Use _latest_preview_legacy for deserialization of old state
    _latest_preview_legacy: Optional[Dict[str, Any]] = field(default=None, repr=False)
    
    # Phase 7D-2: Rendered technical validator fields
    rendered_technical_result: Optional[Dict[str, Any]] = None
    rendered_technical_history: List[Dict[str, Any]] = field(default_factory=list)

    # Phase 7D-3C: Visual quality review fields
    visual_quality_result: Optional[Dict[str, Any]] = None
    visual_quality_history: List[Dict[str, Any]] = field(default_factory=list)

    # Phase 7D-5A: Image artifact contract
    image_artifact: Optional[Dict[str, Any]] = None

    @property
    def latest_preview(self) -> Optional[Dict[str, Any]]:
        """Derive latest preview from preview_history (durable source of truth).
        
        Falls back to legacy _latest_preview_legacy for backward compatibility
        with old serialized state that had duplicated latest_preview field.
        """
        if self.preview_history:
            return self.preview_history[-1]
        return self._latest_preview_legacy

    def __post_init__(self):
        """初始化後自動設定時間戳。"""
        import datetime
        if self.created_at is None:
            self.created_at = datetime.datetime.now().isoformat()
        if self.updated_at is None:
            self.updated_at = self.created_at

    def to_dict(self) -> Dict[str, Any]:
        """將任務轉換為字典（用於序列化）。"""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "content_type": self.content_type.name if isinstance(self.content_type, ContentType) else self.content_type,
            "status": self.status.name if isinstance(self.status, TaskStatus) else self.status,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "plan": self.plan,
            "research_data": self.research_data,
            "draft_content": self.draft_content,
            "critique_result": self.critique_result,
            "quality_result": self.quality_result,
            "revised_content": self.revised_content,
            "optimized_content": self.optimized_content,
            "final_content": self.final_content,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "image_prompt": self.image_prompt,
            "image_url": self.image_url,
            "hero_image_id": self.hero_image_id,
            "hero_image_url": self.hero_image_url,
            "image_status": self.image_status,
            "seo_title": self.seo_title,
            "seo_description": self.seo_description,
            "seo_keywords": self.seo_keywords,
            "learning_proposals": self.learning_proposals,
            "frontend_request": self.frontend_request,
            "frontend_result": self.frontend_result,
            "frontend_security_result": self.frontend_security_result,
            "frontend_conversion_result": self.frontend_conversion_result,
            "frontend_validation_result": self.frontend_validation_result,
            "frontend_production_quality_result": self.frontend_production_quality_result,
            "frontend_retry_count": self.frontend_retry_count,
            "max_frontend_retries": self.max_frontend_retries,
            "frontend_retry_history": self.frontend_retry_history,
            "final_failed_gate": self.final_failed_gate,
            "final_error": self.final_error,
            "final_feedback": self.final_feedback,
            "final_failure_category": self.final_failure_category,
            "failure_timestamp": self.failure_timestamp,
            "wordpress_id": self.wordpress_id,
            "wordpress_url": self.wordpress_url,
            "client_profile": self.client_profile,
            "approval_policy": self.approval_policy,
            "approval_status": self.approval_status,
            "approval_requested_at": self.approval_requested_at,
            "approval_decided_at": self.approval_decided_at,
            "approval_feedback": self.approval_feedback,
            "approval_decision": self.approval_decision,
            "preview_history": self.preview_history,
            "rendered_technical_result": self.rendered_technical_result,
            "rendered_technical_history": self.rendered_technical_history,
            "visual_quality_result": self.visual_quality_result,
            "visual_quality_history": self.visual_quality_history,
            "image_artifact": self.image_artifact,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        """從字典創建 Task 實例（用於反序列化）。"""
        content_type = data.get("content_type", "BLOG_POST")
        if isinstance(content_type, str):
            content_type = ContentType[content_type]

        status = data.get("status", "PENDING")
        if isinstance(status, str):
            status = TaskStatus[status]

        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description"),
            content_type=content_type,
            status=status,
            priority=data.get("priority", 0),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            completed_at=data.get("completed_at"),
            error_message=data.get("error_message"),
            plan=data.get("plan"),
            research_data=data.get("research_data"),
            draft_content=data.get("draft_content"),
            critique_result=data.get("critique_result"),
            quality_result=data.get("quality_result"),
            revised_content=data.get("revised_content"),
            optimized_content=data.get("optimized_content"),
            final_content=data.get("final_content"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            image_prompt=data.get("image_prompt"),
            image_url=data.get("image_url"),
            hero_image_id=data.get("hero_image_id"),
            hero_image_url=data.get("hero_image_url"),
            image_status=data.get("image_status"),
            seo_title=data.get("seo_title"),
            seo_description=data.get("seo_description"),
            seo_keywords=data.get("seo_keywords"),
            learning_proposals=data.get("learning_proposals", []),
            frontend_request=data.get("frontend_request"),
            frontend_result=data.get("frontend_result"),
            frontend_security_result=data.get("frontend_security_result"),
            frontend_conversion_result=data.get("frontend_conversion_result"),
            frontend_validation_result=data.get("frontend_validation_result"),
            frontend_production_quality_result=data.get("frontend_production_quality_result"),
            frontend_retry_count=data.get("frontend_retry_count", 0),
            max_frontend_retries=data.get("max_frontend_retries", 3),
            frontend_retry_history=data.get("frontend_retry_history", []),
            final_failed_gate=data.get("final_failed_gate"),
            final_error=data.get("final_error"),
            final_feedback=data.get("final_feedback"),
            final_failure_category=data.get("final_failure_category"),
            failure_timestamp=data.get("failure_timestamp"),
            wordpress_id=data.get("wordpress_id"),
            wordpress_url=data.get("wordpress_url"),
            client_profile=data.get("client_profile"),
            approval_policy=data.get("approval_policy"),
            approval_status=data.get("approval_status"),
            approval_requested_at=data.get("approval_requested_at"),
            approval_decided_at=data.get("approval_decided_at"),
            approval_feedback=data.get("approval_feedback"),
            approval_decision=data.get("approval_decision"),
            preview_history=data.get("preview_history", []),
            _latest_preview_legacy=data.get("latest_preview"),
            rendered_technical_result=data.get("rendered_technical_result"),
            rendered_technical_history=data.get("rendered_technical_history", []),
            visual_quality_result=data.get("visual_quality_result"),
            visual_quality_history=data.get("visual_quality_history", []),
            image_artifact=data.get("image_artifact"),
        )


@dataclass
class WorkflowState:
    """工作流程狀態，存儲所有任務和全局狀態。"""
    tasks: Dict[str, Task] = field(default_factory=dict)
    current_task_id: Optional[str] = None
    global_state: Dict[str, Any] = field(default_factory=dict)
    
    def add_task(self, task: Task) -> None:
        """添加新任務。"""
        self.tasks[task.id] = task
        if self.current_task_id is None:
            self.current_task_id = task.id
    
    def get_task(self, task_id: str) -> Optional[Task]:
        """獲取指定任務。"""
        return self.tasks.get(task_id)
    
    def update_task_status(self, task_id: str, status: TaskStatus, error_message: Optional[str] = None) -> None:
        """更新任務狀態。"""
        import datetime
        if task_id in self.tasks:
            self.tasks[task_id].status = status
            self.tasks[task_id].updated_at = datetime.datetime.now().isoformat()
            if error_message:
                self.tasks[task_id].error_message = error_message
            if status == TaskStatus.COMPLETED:
                self.tasks[task_id].completed_at = datetime.datetime.now().isoformat()
    
    def set_current_task(self, task_id: str) -> None:
        """設定當前任務。"""
        if task_id in self.tasks:
            self.current_task_id = task_id
    
    def to_dict(self) -> Dict[str, Any]:
        """將狀態轉換為字典（用於序列化）。"""
        return {
            "tasks": {task_id: task.to_dict() for task_id, task in self.tasks.items()},
            "current_task_id": self.current_task_id,
            "global_state": self.global_state,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowState":
        """從字典創建 WorkflowState 實例（用於反序列化）。"""
        state = cls()
        state.current_task_id = data.get("current_task_id")
        state.global_state = data.get("global_state", {})
        
        for task_id, task_data in data.get("tasks", {}).items():
            task = Task.from_dict(task_data)
            state.tasks[task_id] = task
        
        return state


# 全局工作流程狀態實例
workflow_state = WorkflowState()
