from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List, Any
from datetime import datetime
from models import (
    EntityType, TaskStatus, ApprovalStatus, Role, AgentStatusType, ActivityType,
    AgentSessionStatus, DecisionType, LeaseStatus, ContributionType, DiffReviewStatus,
    ApprovalType, AgentApprovalStatus
)


# Entity Schemas
class EntityBase(BaseModel):
    name: str
    entity_type: EntityType
    email: Optional[EmailStr] = None
    skills: Optional[str] = None


class EntityCreate(EntityBase):
    password: Optional[str] = None  # For humans
    api_key: Optional[str] = None  # For agents
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


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    stage_id: Optional[int] = None
    required_skills: Optional[str] = None
    priority: Optional[int] = None
    version: Optional[int] = None  # For optimistic locking


class TaskResponse(TaskBase):
    id: int
    status: TaskStatus
    project_id: int
    stage_id: Optional[int]
    parent_task_id: Optional[int]
    created_by: Optional[int] = None
    version: int = 0
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


# Auth Schemas
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    entity_id: Optional[int] = None
    entity_type: Optional[EntityType] = None


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
