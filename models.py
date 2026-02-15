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
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    assigned_tasks = relationship("Task", secondary=task_assignments, back_populates="assignees")
    created_projects = relationship("Project", back_populates="creator")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
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
