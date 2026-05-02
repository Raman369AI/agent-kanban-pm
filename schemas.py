from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Any
from datetime import datetime
from models import (
    EntityType, TaskStatus, ApprovalStatus, Role, AgentStatusType, ActivityType,
    AgentSessionStatus, DecisionType, LeaseStatus, ContributionType, DiffReviewStatus,
    ApprovalType, AgentApprovalStatus, ReviewMode
)


# Entity Schemas
class EntityBase(BaseModel):
    name: str
    entity_type: EntityType
    email: Optional[str] = None
    skills: Optional[str] = None


class EntityCreate(EntityBase):
    password: Optional[str] = None  # For humans
    role: Optional[Role] = None


class EntityResponse(EntityBase):
    id: int
    role: Role
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# Project Schemas
class ProjectBase(BaseModel):
    name: str
    description: Optional[str] = None
    path: Optional[str] = None


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    path: Optional[str] = None
    approval_status: Optional[ApprovalStatus] = None


class ProjectResponse(ProjectBase):
    id: int
    creator_id: int
    approval_status: ApprovalStatus
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Stage Schemas
class StageBase(BaseModel):
    name: str
    description: Optional[str] = None
    order: int


class StageCreate(StageBase):
    pass


class StageUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    order: Optional[int] = None


class StageResponse(StageBase):
    id: int
    project_id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Task Schemas
class TaskBase(BaseModel):
    title: str
    description: Optional[str] = None
    required_skills: Optional[str] = None
    priority: int = 0


class TaskCreate(TaskBase):
    project_id: int
    parent_task_id: Optional[int] = None
    stage_id: Optional[int] = None
    sequence_order: Optional[int] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    stage_id: Optional[int] = None
    required_skills: Optional[str] = None
    priority: Optional[int] = None
    sequence_order: Optional[int] = None
    version: Optional[int] = None  # For optimistic locking


class TaskResponse(TaskBase):
    id: int
    status: TaskStatus
    project_id: int
    stage_id: Optional[int]
    parent_task_id: Optional[int]
    created_by: Optional[int] = None
    version: int = 0
    sequence_order: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]
    assignees: Optional[List[EntityResponse]] = []

    @field_validator('assignees', mode='before')
    @classmethod
    def ensure_assignees_list(cls, v: Any) -> Any:
        return v if v is not None else []

    class Config:
        from_attributes = True


# Comment Schemas
class CommentBase(BaseModel):
    content: str


class CommentCreate(CommentBase):
    task_id: int


class CommentResponse(CommentBase):
    id: int
    task_id: int
    author_id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Assignment Schema
class TaskAssignment(BaseModel):
    task_id: int
    entity_id: int


class TaskLogResponse(BaseModel):
    id: int
    task_id: int
    message: str
    log_type: str
    created_at: datetime

    class Config:
        from_attributes = True


# Detailed Project Response with nested data
class ProjectDetailResponse(ProjectResponse):
    stages: List[StageResponse] = []
    tasks: List[TaskResponse] = []

    class Config:
        from_attributes = True


# Task Detail with subtasks
class TaskDetailResponse(TaskResponse):
    subtasks: Optional[List[TaskResponse]] = []
    comments: Optional[List[CommentResponse]] = []
    logs: Optional[List[TaskLogResponse]] = []

    @field_validator('subtasks', 'comments', 'logs', mode='before')
    @classmethod
    def ensure_list(cls, v: Any) -> Any:
        return v if v is not None else []

    class Config:
        from_attributes = True


# Agent Activity Schemas
class AgentHeartbeatResponse(BaseModel):
    id: int
    agent_id: int
    task_id: Optional[int] = None
    status_type: AgentStatusType
    message: Optional[str] = None
    updated_at: datetime

    class Config:
        from_attributes = True


class AgentStatusUpdate(BaseModel):
    status_type: AgentStatusType
    message: Optional[str] = None
    task_id: Optional[int] = None


class AgentActivityCreate(BaseModel):
    message: str
    project_id: Optional[int] = None
    session_id: Optional[int] = None
    task_id: Optional[int] = None
    activity_type: ActivityType = ActivityType.ACTION
    source: Optional[str] = None
    payload_json: Optional[str] = None
    workspace_path: Optional[str] = None
    file_path: Optional[str] = None
    command: Optional[str] = None


class AgentActivityResponse(BaseModel):
    id: int
    agent_id: int
    session_id: Optional[int] = None
    project_id: Optional[int] = None
    task_id: Optional[int] = None
    activity_type: ActivityType
    source: Optional[str] = None
    message: str
    payload_json: Optional[str] = None
    workspace_path: Optional[str] = None
    file_path: Optional[str] = None
    command: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AgentSessionCreate(BaseModel):
    project_id: int
    task_id: Optional[int] = None
    workspace_path: Optional[str] = None
    command: Optional[str] = None
    model: Optional[str] = None
    mode: Optional[str] = None


class AgentSessionUpdate(BaseModel):
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE
    message: Optional[str] = None
    task_id: Optional[int] = None


class AgentSessionResponse(BaseModel):
    id: int
    agent_id: int
    project_id: int
    task_id: Optional[int] = None
    workspace_path: str
    status: AgentSessionStatus
    command: Optional[str] = None
    model: Optional[str] = None
    mode: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    last_seen_at: datetime

    class Config:
        from_attributes = True


class ProjectWorkspaceCreate(BaseModel):
    project_id: int
    root_path: str
    label: Optional[str] = None
    is_primary: bool = False
    allowed_patterns: Optional[str] = None
    blocked_patterns: Optional[str] = None


class ProjectWorkspaceResponse(ProjectWorkspaceCreate):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class OrchestrationDecisionCreate(BaseModel):
    project_id: int
    manager_agent_id: Optional[int] = None
    decision_type: DecisionType = DecisionType.OTHER
    input_summary: Optional[str] = None
    rationale: str
    affected_task_ids: Optional[str] = None
    affected_agent_ids: Optional[str] = None


class OrchestrationDecisionResponse(OrchestrationDecisionCreate):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class TaskLeaseCreate(BaseModel):
    task_id: int
    agent_id: Optional[int] = None
    session_id: Optional[int] = None
    ttl_seconds: int = 1800


class TaskLeaseResponse(BaseModel):
    id: int
    task_id: int
    agent_id: int
    session_id: Optional[int] = None
    status: LeaseStatus
    expires_at: datetime
    created_at: datetime
    released_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ActivitySummaryCreate(BaseModel):
    project_id: int
    task_id: Optional[int] = None
    agent_id: Optional[int] = None
    summary: str
    from_activity_id: Optional[int] = None
    to_activity_id: Optional[int] = None


class ActivitySummaryResponse(ActivitySummaryCreate):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class AgentCheckpointCreate(BaseModel):
    project_id: int
    task_id: int
    agent_id: Optional[int] = None
    session_id: Optional[int] = None
    workspace_path: Optional[str] = None
    summary: str
    terminal_tail: Optional[str] = None
    payload_json: Optional[str] = None


class AgentCheckpointResponse(BaseModel):
    id: int
    agent_id: int
    project_id: int
    task_id: int
    session_id: Optional[int] = None
    workspace_path: Optional[str] = None
    summary: str
    terminal_tail: Optional[str] = None
    payload_json: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class UserContributionCreate(BaseModel):
    project_id: int
    entity_id: Optional[int] = None
    contribution_type: ContributionType
    provider: str = "github"
    external_id: Optional[str] = None
    title: str
    url: Optional[str] = None
    status: Optional[str] = None
    created_at_external: Optional[datetime] = None
    updated_at_external: Optional[datetime] = None


class UserContributionResponse(UserContributionCreate):
    id: int
    recorded_at: datetime

    class Config:
        from_attributes = True


class AgentTerminalResponse(BaseModel):
    session: AgentSessionResponse
    activities: List[AgentActivityResponse]


class AgentApprovalCreate(BaseModel):
    project_id: int
    task_id: Optional[int] = None
    session_id: Optional[int] = None
    agent_id: Optional[int] = None
    approval_type: ApprovalType = ApprovalType.OTHER
    title: str
    message: str
    command: Optional[str] = None
    diff_content: Optional[str] = None
    payload_json: Optional[str] = None


class AgentApprovalResolve(BaseModel):
    decision: AgentApprovalStatus  # approved | rejected | cancelled
    response_message: Optional[str] = None


class AgentApprovalResponse(BaseModel):
    id: int
    project_id: int
    task_id: Optional[int] = None
    session_id: Optional[int] = None
    agent_id: int
    approval_type: ApprovalType
    title: str
    message: str
    command: Optional[str] = None
    diff_content: Optional[str] = None
    payload_json: Optional[str] = None
    status: AgentApprovalStatus
    requested_at: datetime
    resolved_at: Optional[datetime] = None
    resolved_by_entity_id: Optional[int] = None
    response_message: Optional[str] = None

    class Config:
        from_attributes = True


class DiffReviewCreate(BaseModel):
    project_id: int
    task_id: Optional[int] = None
    reviewer_id: Optional[int] = None
    requester_id: Optional[int] = None
    diff_content: str
    summary: Optional[str] = None
    file_paths: Optional[str] = None
    is_critical: bool = False


class DiffReviewUpdate(BaseModel):
    status: DiffReviewStatus
    review_notes: Optional[str] = None


class DiffReviewResponse(BaseModel):
    id: int
    project_id: int
    task_id: Optional[int] = None
    reviewer_id: Optional[int] = None
    requester_id: Optional[int] = None
    diff_content: str
    summary: Optional[str] = None
    file_paths: Optional[str] = None
    status: DiffReviewStatus
    review_notes: Optional[str] = None
    is_critical: bool
    created_at: datetime
    reviewed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Agent Connection Schemas
class AgentConnectionCreate(BaseModel):
    protocol: str  # websocket, webhook, mcp, a2a
    config: dict = {}  # {"webhook_url": "..."} or {"a2a_endpoint": "..."}
    subscribed_events: list[str] = ["task_moved", "task_assigned", "task_created"]
    subscribed_projects: list[int] | None = None  # null = all projects


class AgentConnectionResponse(BaseModel):
    id: int
    entity_id: int
    protocol: str
    config: dict
    subscribed_events: list[str]
    subscribed_projects: list[int] | None
    status: str
    last_seen: Optional[datetime] = None

    class Config:
        from_attributes = True


# Chat Designer Schemas
ALLOWED_ROLE_HINTS = {
    "orchestrator", "ui", "architecture", "worker",
    "test", "diff_review", "git_pr",
}


class ChatPlanItem(BaseModel):
    title: str
    description: str = ""
    priority: int = 5
    role_hint: Optional[str] = None
    acceptance: List[str] = Field(default_factory=list)
    depends_on: List[int] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title must be non-empty")
        return v[:255]

    @field_validator("priority")
    @classmethod
    def _priority_range(cls, v: int) -> int:
        if v < 0 or v > 10:
            raise ValueError("priority must be in [0, 10]")
        return v

    @field_validator("role_hint")
    @classmethod
    def _role_hint_allowed(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower() or None
        if v is not None and v not in ALLOWED_ROLE_HINTS:
            raise ValueError(f"role_hint must be one of {sorted(ALLOWED_ROLE_HINTS)} or null")
        return v


class ChatPlanRequest(BaseModel):
    project_id: int
    message: str
    items: Optional[List[ChatPlanItem]] = None
    transcript: Optional[str] = None


# Stage Policy Schemas
class StagePolicyCreate(BaseModel):
    project_id: int
    stage_id: int
    stage_key: str
    on_enter_roles: List[str] = Field(default_factory=list)
    required_outputs: List[str] = Field(default_factory=list)
    review_mode: Optional[ReviewMode] = ReviewMode.NONE
    allow_parallel: bool = False
    requires_orchestrator_move: bool = True


class StagePolicyUpdate(BaseModel):
    stage_key: Optional[str] = None
    on_enter_roles: Optional[List[str]] = None
    required_outputs: Optional[List[str]] = None
    review_mode: Optional[ReviewMode] = None
    allow_parallel: Optional[bool] = None
    requires_orchestrator_move: Optional[bool] = None


class StagePolicyResponse(BaseModel):
    id: int
    project_id: int
    stage_id: int
    stage_key: str
    on_enter_roles: List[str] = Field(default_factory=list)
    required_outputs: List[str] = Field(default_factory=list)
    review_mode: Optional[ReviewMode] = None
    allow_parallel: bool = False
    requires_orchestrator_move: bool = True
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

    @classmethod
    def from_model(cls, obj) -> "StagePolicyResponse":
        import json as _json
        roles = _json.loads(obj.on_enter_roles_json) if obj.on_enter_roles_json else []
        outputs = _json.loads(obj.required_outputs_json) if obj.required_outputs_json else []
        return cls(
            id=obj.id,
            project_id=obj.project_id,
            stage_id=obj.stage_id,
            stage_key=obj.stage_key,
            on_enter_roles=roles,
            required_outputs=outputs,
            review_mode=obj.review_mode,
            allow_parallel=obj.allow_parallel,
            requires_orchestrator_move=obj.requires_orchestrator_move,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )
