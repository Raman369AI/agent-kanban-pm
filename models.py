from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Table, Enum as SQLEnum
from sqlalchemy.orm import relationship, declarative_base
import enum

Base = declarative_base()

# Association table for task assignments
task_assignments = Table(
    'task_assignments',
    Base.metadata,
    Column('task_id', Integer, ForeignKey('tasks.id', ondelete='CASCADE')),
    Column('entity_id', Integer, ForeignKey('entities.id', ondelete='CASCADE'))
)


class EntityType(str, enum.Enum):
    HUMAN = "human"
    AGENT = "agent"


class Role(str, enum.Enum):
    OWNER = "owner"      # Full control, can manage other users
    MANAGER = "manager"  # Can approve/reject projects, manage assignments
    WORKER = "worker"    # Can work on tasks, self-assign
    VIEWER = "viewer"    # Read-only access


class Scope(str, enum.Enum):
    """API-key scope, distinct from Role. Tied to the key, not the entity."""
    OWNER = "owner"
    MANAGER = "manager"
    WORKER = "worker"
    READONLY = "readonly"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Entity(Base):
    """Unified model for both humans and agents"""
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    entity_type = Column(SQLEnum(EntityType), nullable=False)
    email = Column(String(255), unique=True, nullable=True)
    api_key = Column(String(255), unique=True, nullable=True)  # For agent authentication
    hashed_password = Column(String(255), nullable=True)  # For human authentication
    skills = Column(Text, nullable=True)  # Comma-separated skills
    role = Column(SQLEnum(Role), default=Role.WORKER, nullable=False)
    scope = Column(SQLEnum(Scope), nullable=True)  # API-key scope; defaults to role if unset
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    assigned_tasks = relationship("Task", secondary=task_assignments, back_populates="assignees")
    created_projects = relationship("Project", back_populates="creator")
    connections = relationship("AgentConnection", back_populates="entity", cascade="all, delete-orphan")


class ProtocolType(str, enum.Enum):
    WEBSOCKET = "websocket"
    WEBHOOK = "webhook"
    MCP = "mcp"
    A2A = "a2a"


class ConnectionStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class AgentConnection(Base):
    __tablename__ = "agent_connections"

    id = Column(Integer, primary_key=True)
    entity_id = Column(Integer, ForeignKey('entities.id', ondelete='CASCADE'))
    protocol = Column(SQLEnum(ProtocolType), nullable=False)
    config = Column(Text)  # JSON: webhook_url, a2a_endpoint, etc.
    subscribed_events = Column(Text)  # JSON array: ["task_moved", "task_assigned"]
    subscribed_projects = Column(Text)  # JSON array of project IDs, null = all
    status = Column(SQLEnum(ConnectionStatus), default=ConnectionStatus.OFFLINE)
    last_seen = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    entity = relationship("Entity", back_populates="connections")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    path = Column(Text, nullable=True)  # Filesystem path for folder-as-project
    creator_id = Column(Integer, ForeignKey('entities.id'))
    approval_status = Column(SQLEnum(ApprovalStatus), default=ApprovalStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    creator = relationship("Entity", back_populates="created_projects")
    tasks = relationship("Task", back_populates="project", cascade="all, delete-orphan")
    stages = relationship("Stage", back_populates="project", cascade="all, delete-orphan", order_by="Stage.order")


class Stage(Base):
    __tablename__ = "stages"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    order = Column(Integer, nullable=False)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'))
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    project = relationship("Project", back_populates="stages")
    tasks = relationship("Task", back_populates="stage")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(SQLEnum(TaskStatus), default=TaskStatus.PENDING)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'))
    stage_id = Column(Integer, ForeignKey('stages.id', ondelete='SET NULL'), nullable=True)
    parent_task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True)
    required_skills = Column(Text, nullable=True)  # Comma-separated skills
    priority = Column(Integer, default=0)
    created_by = Column(Integer, ForeignKey('entities.id'), nullable=True)
    version = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    # Relationships
    project = relationship("Project", back_populates="tasks")
    stage = relationship("Stage", back_populates="tasks")
    assignees = relationship("Entity", secondary=task_assignments, back_populates="assigned_tasks")
    subtasks = relationship("Task", backref="parent_task", remote_side=[id])
    comments = relationship("Comment", back_populates="task", cascade="all, delete-orphan")
    logs = relationship("TaskLog", back_populates="task", cascade="all, delete-orphan")


class TaskLog(Base):
    __tablename__ = "task_logs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'))
    message = Column(Text, nullable=False)
    log_type = Column(String(50), default="info")  # info, error, thought, action
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    task = relationship("Task", back_populates="logs")


class AgentStatusType(str, enum.Enum):
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    BLOCKED = "blocked"
    WAITING = "waiting"
    DONE = "done"


class ActivityType(str, enum.Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    RESULT = "result"
    ERROR = "error"
    FILE_CHANGE = "file_change"
    COMMAND = "command"
    TOOL_CALL = "tool_call"
    HANDOFF = "handoff"


class DecisionType(str, enum.Enum):
    TASK_ASSIGN = "task_assign"
    TASK_REASSIGN = "task_reassign"
    TASK_SPLIT = "task_split"
    APPROVAL_REQUEST = "approval_request"
    PRIORITY_CHANGE = "priority_change"
    HANDOFF = "handoff"
    OTHER = "other"


class LeaseStatus(str, enum.Enum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class ContributionType(str, enum.Enum):
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    COMMIT = "commit"
    REVIEW = "review"


class AgentSessionStatus(str, enum.Enum):
    STARTING = "starting"
    ACTIVE = "active"
    IDLE = "idle"
    BLOCKED = "blocked"
    DONE = "done"
    ERROR = "error"


class AgentHeartbeat(Base):
    """Current agent state — upserted by workers, polled by manager."""
    __tablename__ = "agent_heartbeats"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey('entities.id', ondelete='CASCADE'), unique=True, nullable=False)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True)
    status_type = Column(SQLEnum(AgentStatusType), default=AgentStatusType.IDLE, nullable=False)
    message = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    agent = relationship("Entity")
    task = relationship("Task")


class AgentSession(Base):
    """A durable CLI-agent run scoped to a project workspace."""
    __tablename__ = "agent_sessions"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey('entities.id', ondelete='CASCADE'), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True, index=True)
    workspace_path = Column(Text, nullable=False)
    status = Column(SQLEnum(AgentSessionStatus), default=AgentSessionStatus.ACTIVE, nullable=False)
    command = Column(Text, nullable=True)
    model = Column(String(255), nullable=True)
    mode = Column(String(50), nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)

    agent = relationship("Entity")
    project = relationship("Project")
    task = relationship("Task")


class ProjectWorkspace(Base):
    """One project can have multiple local workspaces/repos."""
    __tablename__ = "project_workspaces"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True)
    root_path = Column(Text, nullable=False)
    label = Column(String(255), nullable=True)
    is_primary = Column(Boolean, default=False, nullable=False)
    allowed_patterns = Column(Text, nullable=True)
    blocked_patterns = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project")


class OrchestrationDecision(Base):
    """Durable manager-agent rationale for routing and coordination decisions."""
    __tablename__ = "orchestration_decisions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True)
    manager_agent_id = Column(Integer, ForeignKey('entities.id', ondelete='SET NULL'), nullable=True, index=True)
    decision_type = Column(SQLEnum(DecisionType), default=DecisionType.OTHER, nullable=False)
    input_summary = Column(Text, nullable=True)
    rationale = Column(Text, nullable=False)
    affected_task_ids = Column(Text, nullable=True)
    affected_agent_ids = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project")
    manager_agent = relationship("Entity")


class TaskLease(Base):
    """Active work claim for a task/session, separate from assignment."""
    __tablename__ = "task_leases"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey('entities.id', ondelete='CASCADE'), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey('agent_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    status = Column(SQLEnum(LeaseStatus), default=LeaseStatus.ACTIVE, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    released_at = Column(DateTime, nullable=True)

    task = relationship("Task")
    agent = relationship("Entity")
    session = relationship("AgentSession")


class ActivitySummary(Base):
    """Human-readable rollup over a noisy activity range."""
    __tablename__ = "activity_summaries"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True, index=True)
    agent_id = Column(Integer, ForeignKey('entities.id', ondelete='SET NULL'), nullable=True, index=True)
    summary = Column(Text, nullable=False)
    from_activity_id = Column(Integer, ForeignKey('agent_activities.id', ondelete='SET NULL'), nullable=True)
    to_activity_id = Column(Integer, ForeignKey('agent_activities.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project")
    task = relationship("Task")
    agent = relationship("Entity")


class AgentActivity(Base):
    """Append-only activity log for agents."""
    __tablename__ = "agent_activities"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey('entities.id', ondelete='CASCADE'), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey('agent_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True)
    activity_type = Column(SQLEnum(ActivityType), nullable=False)
    source = Column(String(100), nullable=True)
    message = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=True)
    workspace_path = Column(Text, nullable=True)
    file_path = Column(Text, nullable=True)
    command = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    agent = relationship("Entity")
    session = relationship("AgentSession")
    project = relationship("Project")
    task = relationship("Task")


class UserContribution(Base):
    """External contribution artifact such as a GitHub issue, PR, review, or commit."""
    __tablename__ = "user_contributions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True)
    entity_id = Column(Integer, ForeignKey('entities.id', ondelete='SET NULL'), nullable=True, index=True)
    contribution_type = Column(SQLEnum(ContributionType), nullable=False)
    provider = Column(String(50), default="github", nullable=False)
    external_id = Column(String(255), nullable=True)
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=True)
    status = Column(String(50), nullable=True)
    created_at_external = Column(DateTime, nullable=True)
    updated_at_external = Column(DateTime, nullable=True)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project")
    entity = relationship("Entity")


class PendingEvent(Base):
    """Persisted event queue — shared between FastAPI server and MCP server processes."""
    __tablename__ = "pending_events"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey('entities.id', ondelete='CASCADE'), index=True)
    event_type = Column(String(100), nullable=False)
    payload = Column(Text, nullable=False)  # JSON
    project_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DiffReviewStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class ApprovalType(str, enum.Enum):
    SHELL_COMMAND = "shell_command"
    FILE_WRITE = "file_write"
    NETWORK_ACCESS = "network_access"
    GIT_PUSH = "git_push"
    PR_CREATE = "pr_create"
    TOOL_CALL = "tool_call"
    EXTERNAL_ACCESS = "external_access"
    OTHER = "other"


class AgentApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AgentApproval(Base):
    """Durable approval queue for headless CLI agent prompts.

    A running CLI agent that hits a permission prompt (run shell, write file,
    create PR, etc.) blocks until the human resolves the matching row.
    """
    __tablename__ = "agent_approvals"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True, index=True)
    session_id = Column(Integer, ForeignKey('agent_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    agent_id = Column(Integer, ForeignKey('entities.id', ondelete='CASCADE'), nullable=False, index=True)
    approval_type = Column(SQLEnum(ApprovalType), default=ApprovalType.OTHER, nullable=False)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    command = Column(Text, nullable=True)
    diff_content = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    status = Column(SQLEnum(AgentApprovalStatus), default=AgentApprovalStatus.PENDING, nullable=False, index=True)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by_entity_id = Column(Integer, ForeignKey('entities.id', ondelete='SET NULL'), nullable=True)
    response_message = Column(Text, nullable=True)

    project = relationship("Project")
    task = relationship("Task")
    session = relationship("AgentSession")
    agent = relationship("Entity", foreign_keys=[agent_id])
    resolved_by = relationship("Entity", foreign_keys=[resolved_by_entity_id])


class DiffReview(Base):
    """Critical diff review gate — required before security-sensitive code lands."""
    __tablename__ = "diff_reviews"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True, index=True)
    reviewer_id = Column(Integer, ForeignKey('entities.id', ondelete='SET NULL'), nullable=True, index=True)
    requester_id = Column(Integer, ForeignKey('entities.id', ondelete='SET NULL'), nullable=True)
    diff_content = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    file_paths = Column(Text, nullable=True)
    status = Column(SQLEnum(DiffReviewStatus), default=DiffReviewStatus.PENDING, nullable=False)
    review_notes = Column(Text, nullable=True)
    is_critical = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)

    project = relationship("Project")
    task = relationship("Task")
    reviewer = relationship("Entity", foreign_keys=[reviewer_id])
    requester = relationship("Entity", foreign_keys=[requester_id])


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'))
    author_id = Column(Integer, ForeignKey('entities.id'))
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    task = relationship("Task", back_populates="comments")
    author = relationship("Entity")
