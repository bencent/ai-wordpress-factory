# AI WordPress Factory 工作白板定義
# 存儲任務狀態、數據結構和工作流程的中間數據

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from enum import Enum, auto


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
    PUBLISHING = auto()    # 發布中
    COMPLETED = auto()     # 已完成
    FAILED = auto()        # 失敗


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
    revised_content: Optional[str] = None
    optimized_content: Optional[str] = None
    final_content: Optional[str] = None
    
    # Retry 保護
    retry_count: int = 0
    max_retries: int = 3
    
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
    
    # 發布相關
    wordpress_id: Optional[int] = None
    wordpress_url: Optional[str] = None
    
    def __post_init__(self):
        """初始化後自動設定時間戳。"""
        import datetime
        if self.created_at is None:
            self.created_at = datetime.datetime.now().isoformat()
        if self.updated_at is None:
            self.updated_at = self.created_at


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
            "tasks": {task_id: task.__dict__ for task_id, task in self.tasks.items()},
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
            task = Task(
                id=task_data["id"],
                title=task_data["title"],
                description=task_data.get("description"),
                content_type=ContentType[task_data.get("content_type", "BLOG_POST")],
                status=TaskStatus[task_data.get("status", "PENDING")],
                priority=task_data.get("priority", 0),
                created_at=task_data.get("created_at"),
                updated_at=task_data.get("updated_at"),
                completed_at=task_data.get("completed_at"),
                error_message=task_data.get("error_message"),
                plan=task_data.get("plan"),
                research_data=task_data.get("research_data"),
                draft_content=task_data.get("draft_content"),
                critique_result=task_data.get("critique_result"),
                revised_content=task_data.get("revised_content"),
                optimized_content=task_data.get("optimized_content"),
                final_content=task_data.get("final_content"),
                retry_count=task_data.get("retry_count", 0),
                max_retries=task_data.get("max_retries", 3),
                image_prompt=task_data.get("image_prompt"),
                image_url=task_data.get("image_url"),
                hero_image_id=task_data.get("hero_image_id"),
                hero_image_url=task_data.get("hero_image_url"),
                image_status=task_data.get("image_status"),
                seo_title=task_data.get("seo_title"),
                seo_description=task_data.get("seo_description"),
                seo_keywords=task_data.get("seo_keywords"),
                learning_proposals=task_data.get("learning_proposals", []),
                wordpress_id=task_data.get("wordpress_id"),
                wordpress_url=task_data.get("wordpress_url"),
            )
            state.tasks[task_id] = task
        
        return state


# 全局工作流程狀態實例
workflow_state = WorkflowState()
