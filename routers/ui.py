from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload, joinedload
from typing import List, Optional
from datetime import datetime

from database import get_db
from models import Project, Task, Entity, Stage, Comment, EntityType, TaskStatus, ApprovalStatus
from auth import get_password_hash

# Initialize templates
templates = Jinja2Templates(directory="templates")

router = APIRouter(include_in_schema=False)

# Global state for UI settings
auto_pilot_enabled = False


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """Dashboard page"""
    # Get stats
    total_projects = await db.execute(select(func.count(Project.id)))
    total_tasks = await db.execute(select(func.count(Task.id)))
    completed_tasks = await db.execute(select(func.count(Task.id)).where(Task.status == TaskStatus.COMPLETED))
    total_entities = await db.execute(select(func.count(Entity.id)))
    
    stats = {
        "total_projects": total_projects.scalar(),
        "total_tasks": total_tasks.scalar(),
        "completed_tasks": completed_tasks.scalar(),
        "total_entities": total_entities.scalar()
    }
    
    # Get recent projects
    result = await db.execute(
        select(Project).order_by(Project.created_at.desc()).limit(5)
    )
    recent_projects = result.scalars().all()
    
    # Add task count to projects
    for project in recent_projects:
        task_count_result = await db.execute(
            select(func.count(Task.id)).where(Task.project_id == project.id)
        )
        project.task_count = task_count_result.scalar()
    
    # Get recent tasks
    result = await db.execute(
        select(Task).order_by(Task.created_at.desc()).limit(6)
    )
    recent_tasks = result.scalars().all()
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "recent_projects": recent_projects,
        "recent_tasks": recent_tasks
    })


@router.get("/ui/projects", response_class=HTMLResponse)
async def ui_projects(request: Request, db: AsyncSession = Depends(get_db)):
    """Projects list page"""
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.stages), selectinload(Project.tasks))
        .order_by(Project.created_at.desc())
    )
    projects = result.scalars().all()
    
    # Get available agents for the create form
    agents_result = await db.execute(
        select(Entity).filter(Entity.entity_type == EntityType.AGENT, Entity.is_active == True)
    )
    agents = agents_result.scalars().all()
    
    return templates.TemplateResponse("projects.html", {
        "request": request,
        "projects": projects,
        "agents": agents
    })


@router.get("/ui/projects/{project_id}/board", response_class=HTMLResponse)
async def project_kanban_board(request: Request, project_id: int, db: AsyncSession = Depends(get_db)):
    """Kanban board for a project"""
    result = await db.execute(
        select(Project)
        .filter(Project.id == project_id)
        .options(
            selectinload(Project.stages).selectinload(Stage.tasks).joinedload(Task.assignees),
            selectinload(Project.tasks).joinedload(Task.assignees),
            selectinload(Project.creator)
        )
    )
    project = result.scalar_one_or_none()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    return templates.TemplateResponse("kanban_board.html", {
        "request": request,
        "project": project
    })


@router.patch("/ui/tasks/{task_id}/move")
async def ui_move_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Move a task to a different stage (UI drag-and-drop, no auth required)"""
    body = await request.json()
    new_stage_id = body.get("stage_id")
    new_status = body.get("status", "pending")
    
    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task.stage_id = new_stage_id
    task.status = new_status
    if new_status == "completed" and task.completed_at is None:
        task.completed_at = datetime.utcnow()
    task.updated_at = datetime.utcnow()
    
    await db.commit()
    return {"ok": True, "task_id": task_id, "stage_id": new_stage_id, "status": new_status}


@router.get("/ui/api/settings")
async def ui_get_settings():
    """Get current app settings"""
    return {"auto_pilot": auto_pilot_enabled}


@router.post("/ui/api/settings/auto-pilot")
async def ui_toggle_auto_pilot(request: Request):
    """Toggle auto-pilot mode"""
    global auto_pilot_enabled
    body = await request.json()
    auto_pilot_enabled = body.get("enabled", False)
    return {"ok": True, "auto_pilot": auto_pilot_enabled}


@router.get("/ui/api/entities")
async def ui_list_entities(db: AsyncSession = Depends(get_db)):
    """List all entities for UI dropdowns"""
    result = await db.execute(select(Entity).filter(Entity.is_active == True))
    entities = result.scalars().all()
    return [{"id": e.id, "name": e.name, "entity_type": e.entity_type, "skills": e.skills} for e in entities]


@router.post("/ui/tasks/create")
async def ui_create_task(request: Request, db: AsyncSession = Depends(get_db)):
    """Create a task from the UI"""
    body = await request.json()
    
    task = Task(
        title=body["title"],
        description=body.get("description", ""),
        project_id=body["project_id"],
        stage_id=body["stage_id"],
        priority=body.get("priority", 0),
        required_skills=body.get("required_skills", ""),
        status=body.get("status", "pending")
    )
    db.add(task)
    await db.commit()
    await db.refresh(task, ["assignees"])
    
    return {
        "ok": True,
        "task": {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "priority": task.priority,
            "required_skills": task.required_skills,
            "assignees": []
        }
    }


@router.patch("/ui/tasks/{task_id}/edit")
async def ui_edit_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Edit a task from the UI"""
    body = await request.json()
    
    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    for field in ["title", "description", "priority", "required_skills", "status"]:
        if field in body:
            setattr(task, field, body[field])
    
    if body.get("status") == "completed" and task.completed_at is None:
        task.completed_at = datetime.utcnow()
    
    task.updated_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@router.delete("/ui/tasks/{task_id}")
async def ui_delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a task from the UI"""
    result = await db.execute(select(Task).filter(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    await db.delete(task)
    await db.commit()
    return {"ok": True}


@router.post("/ui/tasks/{task_id}/assign")
async def ui_assign_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Assign/unassign entities to a task from the UI"""
    body = await request.json()
    entity_id = body["entity_id"]
    action = body.get("action", "assign")
    
    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = entity_result.scalar_one_or_none()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    
    if action == "assign" and entity not in task.assignees:
        task.assignees.append(entity)
    elif action == "unassign" and entity in task.assignees:
        task.assignees.remove(entity)
    
    await db.commit()
    return {"ok": True}


@router.patch("/ui/projects/{project_id}/edit")
async def ui_edit_project(project_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Edit a project from the UI"""
    body = await request.json()
    
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    for field in ["name", "description", "approval_status"]:
        if field in body:
            setattr(project, field, body[field])
    
    project.updated_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@router.delete("/ui/projects/{project_id}")
async def ui_delete_project(project_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a project from the UI"""
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    await db.delete(project)
    await db.commit()
    return {"ok": True}


@router.get("/ui/register", response_class=HTMLResponse)
async def ui_register_form(request: Request):
    """Show human registration form"""
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/ui/register", response_class=HTMLResponse)
async def ui_register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    skills: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    """Handle human registration form submission"""
    # Check if email already exists
    result = await db.execute(select(Entity).filter(Entity.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "Email already registered"
        })
    
    db_entity = Entity(
        name=name,
        entity_type=EntityType.HUMAN,
        email=email,
        hashed_password=get_password_hash(password),
        skills=skills
    )
    db.add(db_entity)
    await db.commit()
    
    return templates.TemplateResponse("register.html", {
        "request": request,
        "success": f"Account created for {name}! You can now use the API with your credentials."
    })


@router.post("/ui/projects/create", response_class=HTMLResponse)
async def ui_create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    """Create a new project from the UI with optional agent assignment"""
    form = await request.form()
    agent_ids = form.getlist("agent_ids")
    
    # Create the project
    project = Project(
        name=name,
        description=description,
        creator_id=1,
        approval_status=ApprovalStatus.APPROVED
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    
    # Create default stages
    default_stages = [
        ("Backlog", "Tasks to be done", 1),
        ("To Do", "Ready to start", 2),
        ("In Progress", "Currently being worked on", 3),
        ("Review", "Awaiting review", 4),
        ("Done", "Completed tasks", 5),
    ]
    for stage_name, stage_desc, order in default_stages:
        stage = Stage(
            name=stage_name,
            description=stage_desc,
            order=order,
            project_id=project.id
        )
        db.add(stage)
    await db.commit()
    
    # Notify assigned agents (simplified - just refresh project for now)
    if agent_ids:
        # assignment logic would go here
        pass
    
    return RedirectResponse(url=f"/ui/projects/{project.id}/board", status_code=303)


@router.get("/ui/users", response_class=HTMLResponse)
async def ui_users(request: Request, db: AsyncSession = Depends(get_db)):
    """List all registered users (entities)"""
    result = await db.execute(select(Entity).order_by(Entity.created_at.desc()))
    users = result.scalars().all()
    
    return templates.TemplateResponse("users.html", {
        "request": request,
        "users": users
    })
