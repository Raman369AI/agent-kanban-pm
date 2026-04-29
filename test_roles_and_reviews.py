"""Tests for the post-baseline AGENTS.md features:
- Diff review gate (model, REST, MCP)
- Role taxonomy in preferences
- Role supervisor
- Chat task creation
- Per-adapter heartbeat thresholds
- Git PR role isolation
"""

import pytest
import os
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from database import init_db, get_db, Base
from models import (
    DiffReview, DiffReviewStatus, Entity, EntityType, Role,
    Project, Stage, ApprovalStatus, Task, TaskStatus, AgentHeartbeat,
    AgentStatusType, AgentSession, AgentSessionStatus,
)
from schemas import DiffReviewCreate, DiffReviewResponse, DiffReviewUpdate
from kanban_runtime.preferences import (
    Preferences, RoleConfig, RoleAssignment, AgentRole,
    ManagerConfig, WorkerConfig, AutonomyConfig,
    load_preferences, save_preferences,
)
from main import app


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///./test_roles.db", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS diff_reviews"))
    await engine.dispose()
    if Path("./test_roles.db").exists():
        Path("./test_roles.db").unlink(missing_ok=True)


@pytest.fixture
async def db_session(db_engine):
    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        yield session


@pytest.fixture
async def client(db_engine):
    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestRolePreferences:
    def test_role_config_parsing(self):
        prefs = Preferences(
            roles=RoleConfig(
                orchestrator=RoleAssignment(agent="claude", mode="headless"),
                worker=RoleAssignment(agent="gemini", mode="headless"),
                diff_review=RoleAssignment(agent="claude", mode="headless"),
            ),
            autonomy=AutonomyConfig(),
        )
        assignments = prefs.get_role_assignments()
        assert "orchestrator" in assignments
        assert assignments["orchestrator"].agent == "claude"
        assert "worker" in assignments
        assert assignments["worker"].agent == "gemini"
        assert "diff_review" in assignments
        assert "ui" not in assignments
        assert "git_pr" not in assignments

    def test_legacy_migration(self):
        prefs = Preferences(
            manager=ManagerConfig(agent="claude", model="claude-sonnet-4-6", mode="auto"),
            workers=[
                WorkerConfig(agent="gemini", roles=["worker"]),
                WorkerConfig(agent="opencode", roles=["worker", "test"]),
            ],
            autonomy=AutonomyConfig(),
        )
        roles = prefs.get_roles()
        assert roles.orchestrator is not None
        assert roles.orchestrator.agent == "claude"
        assert roles.worker is not None
        assert roles.worker.agent == "gemini"
        assert roles.test is not None
        assert roles.test.agent == "opencode"

    def test_agent_role_enum(self):
        assert AgentRole.ORCHESTRATOR.value == "orchestrator"
        assert AgentRole.GIT_PR.value == "git_pr"
        assert AgentRole.DIFF_REVIEW.value == "diff_review"

    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kanban_runtime.preferences.PREFERENCES_PATH", tmp_path / "prefs.yaml")
        prefs = Preferences(
            roles=RoleConfig(
                orchestrator=RoleAssignment(agent="claude", mode="headless", model="claude-sonnet-4-6"),
                git_pr=RoleAssignment(agent="gh", mode="headless"),
            ),
            autonomy=AutonomyConfig(),
        )
        save_preferences(prefs)
        loaded = load_preferences()
        assert loaded is not None
        assert loaded.roles is not None
        assert loaded.roles.orchestrator.agent == "claude"
        assert loaded.roles.git_pr.agent == "gh"

    def test_standalone_cli_role_assignment(self):
        assignment = RoleAssignment(
            agent="my-cli",
            command="my-cli",
            display_name="My CLI",
            mode="headless",
            model="model-a",
            models=["model-a", "model-b"],
            capabilities=["worker"],
        )
        assert assignment.is_standalone_cli is True
        assert assignment.command == "my-cli"
        assert assignment.model == "model-a"
        assert assignment.models == ["model-a", "model-b"]

    def test_standalone_role_adapter_is_in_memory_only(self):
        from kanban_runtime.adapter_loader import standalone_assignment_to_adapter

        assignment = RoleAssignment(
            agent="my-cli",
            command="my-cli",
            display_name="My CLI",
            mode="headless",
            capabilities=["worker"],
        )
        adapter = standalone_assignment_to_adapter("worker", assignment)
        assert adapter.name == "my-cli"
        assert adapter.invoke.command == "my-cli"
        assert adapter.auth.type == "none"
        assert adapter.roles == ["worker"]


class TestDiffReviewModel:
    def test_diff_review_status_enum(self):
        assert DiffReviewStatus.PENDING.value == "pending"
        assert DiffReviewStatus.APPROVED.value == "approved"
        assert DiffReviewStatus.REJECTED.value == "rejected"
        assert DiffReviewStatus.CHANGES_REQUESTED.value == "changes_requested"

    def test_diff_review_create_schema(self):
        review = DiffReviewCreate(
            project_id=1,
            diff_content="diff --git a/auth.py b/auth.py\n+new line",
            summary="Security fix",
            is_critical=True,
        )
        assert review.is_critical is True
        assert review.project_id == 1

    def test_diff_review_update_schema(self):
        update = DiffReviewUpdate(
            status=DiffReviewStatus.APPROVED,
            review_notes="Looks good",
        )
        assert update.status == DiffReviewStatus.APPROVED


class TestDiffReviewAPI:
    def test_create_and_list_diff_reviews(self):
        from fastapi.testclient import TestClient
        from database import init_db

        asyncio.run(init_db())

        with TestClient(app) as client:
            r = client.post("/entities/register/human", json={
                "name": "Review Test Manager",
                "entity_type": "human",
            })
            human_id = r.json()["id"]

            r = client.post("/projects", json={
                "name": "Diff Review Test Project",
                "description": "Testing diff reviews",
            }, headers={"X-Entity-ID": str(human_id)})
            project = r.json()
            project_id = project["id"]

            r = client.post("/projects/{}/approve".format(project_id), headers={"X-Entity-ID": str(human_id)})
            assert r.status_code == 200

            review_data = {
                "project_id": project_id,
                "diff_content": "diff --git a/auth.py b/auth.py\n+new line",
                "summary": "Security fix in auth",
                "is_critical": True,
            }
            r = client.post(
                "/agents/projects/{}/diff-reviews".format(project_id),
                json=review_data,
                headers={"X-Entity-ID": str(human_id)},
            )

            assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
            data = r.json()
            assert data["status"] == "pending"
            assert data["is_critical"] is True
            review_id = data["id"]

            list_r = client.get("/agents/projects/{}/diff-reviews".format(project_id))
            assert list_r.status_code == 200
            reviews = list_r.json()
            assert len(reviews) >= 1

            approve_r = client.patch(
                "/agents/diff-reviews/{}".format(review_id),
                json={"status": "approved", "review_notes": "LGTM"},
                headers={"X-Entity-ID": str(human_id)},
            )
            assert approve_r.status_code == 200
            approved = approve_r.json()
            assert approved["status"] == "approved"
            assert approved["review_notes"] == "LGTM"


class TestApprovalQueueAPI:
    def test_approval_queue_requires_auth_and_manager_resolution(self):
        from fastapi.testclient import TestClient
        from database import init_db

        asyncio.run(init_db())

        with TestClient(app) as client:
            owner_r = client.post("/entities/register/human", json={
                "name": "Approval Test Owner",
                "entity_type": "human",
                "role": "owner",
            })
            owner_id = owner_r.json()["id"]

            worker_r = client.post("/entities/register/human", json={
                "name": "Approval Test Worker",
                "entity_type": "human",
                "role": "worker",
            })
            worker_id = worker_r.json()["id"]

            agent_r = client.post("/entities/register/agent", json={
                "name": "approval-test-agent",
                "entity_type": "agent",
            })
            agent_id = agent_r.json()["id"]

            project_r = client.post("/projects", json={
                "name": "Approval Queue Test Project",
                "description": "Testing approval queue",
            }, headers={"X-Entity-ID": str(owner_id)})
            project_id = project_r.json()["id"]

            unauth_list = client.get(f"/agents/approvals?project_id={project_id}")
            assert unauth_list.status_code == 401

            approval_r = client.post("/agents/approvals", json={
                "project_id": project_id,
                "agent_id": agent_id,
                "approval_type": "shell_command",
                "title": "Run command",
                "message": "Run pytest?",
                "command": "pytest -q",
            }, headers={"X-Entity-ID": str(agent_id)})
            assert approval_r.status_code == 201, approval_r.text
            approval = approval_r.json()
            assert approval["status"] == "pending"
            approval_id = approval["id"]

            worker_list = client.get(
                f"/agents/approvals?project_id={project_id}",
                headers={"X-Entity-ID": str(worker_id)},
            )
            assert worker_list.status_code == 200
            assert worker_list.json() == []

            owner_list = client.get(
                f"/agents/approvals?project_id={project_id}",
                headers={"X-Entity-ID": str(owner_id)},
            )
            assert owner_list.status_code == 200
            assert any(item["id"] == approval_id for item in owner_list.json())

            worker_resolve = client.patch(
                f"/agents/approvals/{approval_id}/resolve",
                json={"decision": "approved"},
                headers={"X-Entity-ID": str(worker_id)},
            )
            assert worker_resolve.status_code == 403

            owner_resolve = client.patch(
                f"/agents/approvals/{approval_id}/resolve",
                json={"decision": "approved", "response_message": "y"},
                headers={"X-Entity-ID": str(owner_id)},
            )
            assert owner_resolve.status_code == 200
            resolved = owner_resolve.json()
            assert resolved["status"] == "approved"
            assert resolved["response_message"] == "y"


class TestAdapterRoleMapping:
    def test_orchestrator_maps_to_manager(self):
        from kanban_runtime.adapter_loader import adapter_role_to_db_role
        assert adapter_role_to_db_role(["orchestrator"]) == Role.MANAGER
        assert adapter_role_to_db_role(["orchestrator", "worker"]) == Role.MANAGER
        assert adapter_role_to_db_role(["manager"]) == Role.MANAGER

    def test_worker_roles_map_to_worker(self):
        from kanban_runtime.adapter_loader import adapter_role_to_db_role
        assert adapter_role_to_db_role(["worker"]) == Role.WORKER
        assert adapter_role_to_db_role(["ui"]) == Role.WORKER
        assert adapter_role_to_db_role(["test"]) == Role.WORKER
        assert adapter_role_to_db_role(["diff_review"]) == Role.WORKER
        assert adapter_role_to_db_role(["git_pr"]) == Role.WORKER

    def test_unknown_maps_to_viewer(self):
        from kanban_runtime.adapter_loader import adapter_role_to_db_role
        assert adapter_role_to_db_role(["unknown"]) == Role.VIEWER


class TestAssignmentLauncher:
    def test_build_agent_command_for_installed_cli_shapes(self, monkeypatch):
        from kanban_runtime.assignment_launcher import _build_agent_command
        from kanban_runtime.adapter_loader import AdapterSpec, InvokeSpec

        monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")
        prompt = "Work on task #1"
        workspace = "/tmp/project"

        gemini = AdapterSpec(
            name="gemini",
            display_name="Gemini",
            invoke=InvokeSpec(command="gemini"),
        )
        assert _build_agent_command(gemini, workspace, prompt) == [
            "/usr/bin/gemini", "--approval-mode", "default", "-i", prompt
        ]

        codex = AdapterSpec(
            name="codex",
            display_name="Codex",
            invoke=InvokeSpec(command="codex"),
        )
        assert _build_agent_command(codex, workspace, prompt) == [
            "/usr/bin/codex", "--ask-for-approval", "on-request", "-C", workspace, prompt
        ]

        opencode = AdapterSpec(
            name="opencode",
            display_name="OpenCode",
            invoke=InvokeSpec(command="opencode"),
        )
        assert _build_agent_command(opencode, workspace, prompt) == [
            "/usr/bin/opencode", "run", prompt
        ]
