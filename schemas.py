from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime
from models import EntityType, TaskStatus, ApprovalStatus


# Entity Schemas
class EntityBase(BaseModel):
    name: str
    entity_type: EntityType
    email: Optional[EmailStr] = None
    skills: Optional[str] = None


class EntityCreate(EntityBase):
    password: Optional[str] = None  # For humans
    api_key: Optional[str] = None  # For agents


class EntityResponse(EntityBase):
    id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# Project Schemas
class ProjectBase(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
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


class TaskResponse(TaskBase):
    id: int
    status: TaskStatus
    project_id: int
    stage_id: Optional[int]
    parent_task_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]
    assignees: List[EntityResponse] = []

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
    subtasks: List[TaskResponse] = []
    comments: List[CommentResponse] = []
    logs: List[TaskLogResponse] = []

    class Config:
        from_attributes = True
