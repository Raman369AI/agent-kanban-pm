#!/usr/bin/env python3
"""
Kanban CLI — init, agents, daemon, run, and role management commands.

Usage:
    python -m kanban_cli init               # Interactive setup wizard
    python -m kanban_cli agents list        # Show installed adapters
    python -m kanban_cli daemon start       # Start manager daemon
    python -m kanban_cli daemon status      # Check daemon status
    python -m kanban_cli daemon stop        # Stop manager daemon
    python -m kanban_cli run                # Start server + UI + supervisor + agents
    python -m kanban_cli roles list         # Show role assignments
    python -m kanban_cli roles assign <role> <agent>  # Assign agent to role
    python -m kanban_cli roles start        # Start all role agents
    python -m kanban_cli roles stop         # Stop all role agents
    python -m kanban_cli roles status       # Show role session status
"""

import argparse
import sys
import os
import subprocess
import time
import shutil
import signal
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kanban_runtime.preferences import (
    Preferences, ManagerConfig, WorkerConfig, AutonomyConfig,
    RoleConfig, RoleAssignment, AgentRole,
    save_preferences, load_preferences, PREFERENCES_PATH,
)
from kanban_runtime.adapter_loader import load_all_adapters, discover_popular_clis


def cmd_init(args):
    print("=" * 60)
    print("KANBAN INITIALIZATION WIZARD")
    print("=" * 60)

    from kanban_runtime.adapter_loader import copy_bundled_adapters
    copy_bundled_adapters()

    adapters = load_all_adapters()
    if not adapters:
        print("No adapters found. Make sure you have agent YAMLs in ~/.kanban/agents/")
        sys.exit(1)

    print("\nAvailable agents:")
    manager_candidates = []
    for i, a in enumerate(adapters, 1):
        roles = ", ".join(a.roles)
        marker = " [can be manager]" if "manager" in a.roles else ""
        print(f"  {i}. {a.display_name} (roles: {roles}){marker}")
        if "manager" in a.roles:
            manager_candidates.append(a)

    if not manager_candidates:
        print("\nNo agent declares 'manager' role. Using first agent as fallback.")
        manager_candidates = adapters[:1]

    print(f"\nSelect orchestrator agent (default: {manager_candidates[0].display_name}):")
    choice = input(f"Enter number or name [{manager_candidates[0].display_name}]: ").strip()
    manager = manager_candidates[0]
    if choice:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(adapters):
                manager = adapters[idx]
        except ValueError:
            for a in adapters:
                if a.name == choice or a.display_name == choice:
                    manager = a
                    break

    orchestrator_model = None
    if manager.models:
        orchestrator_model = manager.models[0].id
    model_input = input(f"Model for orchestrator [{orchestrator_model or 'default'}]: ").strip()
    if model_input:
        orchestrator_model = model_input

    mode_input = input("Orchestrator mode (supervised/auto/headless) [headless]: ").strip().lower()
    mode = mode_input if mode_input in ("supervised", "auto", "headless") else "headless"

    role_configs = {}
    role_defs = [
        ("orchestrator", "Orchestrator Agent", "Owns board movement, assignment, escalation"),
        ("ui", "UI Agent", "Frontend views, UX polish"),
        ("architecture", "Architecture Agent", "Design notes, architectural fit"),
        ("worker", "Worker Agent(s)", "Bulk implementation code"),
        ("test", "Test Writer/Checker", "Tests, smoke/regression checks"),
        ("diff_review", "Diff Checker/Reviewer", "Reviews diffs for behavior risk"),
        ("git_pr", "Git PR Agent", "Branch, commit, push, PR creation"),
    ]

    print("\n--- Role Assignments ---")
    print("For each role, choose an agent or leave blank to skip.\n")

    orchestrator_assignment = RoleAssignment(agent=manager.name, mode=mode, model=orchestrator_model)
    role_configs["orchestrator"] = orchestrator_assignment

    for role_key, role_label, role_desc in role_defs[1:]:
        disabled = ""
        print(f"\n{role_label} — {role_desc}")
        print(f"  Available agents: {', '.join(a.display_name for a in adapters)}")
        agent_choice = input(f"  Agent for {role_key} [skip]: ").strip()
        if not agent_choice:
            continue

        resolved = None
        try:
            idx = int(agent_choice) - 1
            if 0 <= idx < len(adapters):
                resolved = adapters[idx]
        except ValueError:
            for a in adapters:
                if a.name == agent_choice or a.display_name == agent_choice:
                    resolved = a
                    break

        if resolved:
            role_mode = input(f"  Mode for {role_key} (supervised/auto/headless) [headless]: ").strip().lower()
            role_mode = role_mode if role_mode in ("supervised", "auto", "headless") else "headless"
            role_configs[role_key] = RoleAssignment(agent=resolved.name, mode=role_mode)
        else:
            print(f"  No match found for '{agent_choice}'. Skipping {role_key}.")

    autonomy = AutonomyConfig(
        require_approval_for=["project_create", "agent_add"] if mode == "supervised" else [],
        auto_approve=["task_move", "task_assign", "comment"],
    )

    workers = []
    if "worker" in role_configs:
        workers.append(WorkerConfig(agent=role_configs["worker"].agent, roles=["worker"]))

    prefs = Preferences(
        manager=ManagerConfig(
            agent=manager.name,
            model=orchestrator_model or "default",
            mode=mode,
        ),
        workers=workers,
        roles=RoleConfig(**role_configs) if role_configs else None,
        autonomy=autonomy,
    )

    save_preferences(prefs)

    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    for rk, ra in role_configs.items():
        display = ra.agent
        for a in adapters:
            if a.name == ra.agent:
                display = a.display_name
                break
        print(f"  {rk:16s} -> {display} (mode: {ra.mode})")
    print(f"  Config: {PREFERENCES_PATH}")

    print("\nStart the full system with: python -m kanban_cli run")
    print("Or start just the daemon: python -m kanban_cli daemon start")


def cmd_agents_list(args):
    adapters = load_all_adapters()
    if not adapters:
        print("No adapters found in ~/.kanban/agents/")
        return

    print(f"{'Name':<20} {'Display Name':<25} {'Roles':<20} {'Default Model':<24} {'Active'}")
    print("-" * 110)
    for a in adapters:
        available = shutil.which(a.invoke.command) is not None
        status = "yes" if available else "no (CLI missing)"
        default_model = a.models[0].id if a.models else "default"
        print(f"{a.name:<20} {a.display_name:<25} {', '.join(a.roles):<20} {default_model:<24} {status}")


def cmd_agents_discover(args):
    """Check common CLI tools without adding them to the registry."""
    results = discover_popular_clis()
    print("Detected local CLI tools (read-only scan; nothing is registered):")
    print(f"{'Command':<18} {'Display Name':<22} {'Installed':<10} Path")
    print("-" * 90)
    for item in results:
        installed = "yes" if item.installed else "no"
        print(f"{item.command:<18} {item.display_name:<22} {installed:<10} {item.path or '-'}")


def cmd_daemon(args):
    from kanban_runtime.manager_daemon import start_manager_daemon
    start_manager_daemon()


def cmd_daemon_status(args):
    from kanban_runtime.manager_daemon import daemon_status
    status = daemon_status()
    print(f"Manager daemon: {'running' if status['running'] else 'stopped'}")
    if status.get("pid"):
        print(f"  PID: {status['pid']}")
    if status.get("uptime_seconds"):
        print(f"  Uptime: {status['uptime_seconds']}s")
    print(f"  {status['message']}")


def cmd_daemon_stop(args):
    from kanban_runtime.manager_daemon import daemon_stop
    result = daemon_stop()
    print(f"{'OK' if result['success'] else 'ERROR'}: {result['message']}")


def cmd_run(args):
    """Start the full local system: server + UI + role supervisor."""
    api_base = args.api_base or "http://localhost:8000"
    host = args.host or "0.0.0.0"
    port = args.port or 8000
    no_supervisor = args.no_supervisor

    from kanban_runtime.adapter_loader import copy_bundled_adapters
    copy_bundled_adapters()

    print("=" * 60)
    print("KANBAN LOCAL RUNTIME")
    print("=" * 60)

    server_proc = None
    supervisor = None

    def _shutdown(signum=None, frame=None):
        nonlocal server_proc, supervisor
        print("\nShutting down...")
        if supervisor:
            supervisor.stop()
        if server_proc:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"Starting server on {host}:{port}...")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", host, "--port", str(port)],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    time.sleep(2)

    if server_proc.poll() is not None:
        print(f"ERROR: Server failed to start (exit code {server_proc.returncode})")
        sys.exit(1)

    print(f"Server started (PID {server_proc.pid})")
    print(f"API: {api_base}")
    print(f"UI:  http://localhost:{port}")

    if not no_supervisor:
        from kanban_runtime.role_supervisor import RoleSupervisor
        supervisor = RoleSupervisor(api_base=api_base)
        supervisor.start()

        if supervisor.sessions:
            print(f"\nRole sessions started ({len(supervisor.sessions)}):")
            for role_name, session in supervisor.sessions.items():
                session_info = session.tmux_session or f"PID {session.pid}"
                print(f"  {role_name:16s} -> {session.agent} ({session_info})")

            if shutil.which("tmux"):
                print(f"\nAttach to sessions with: tmux attach -t kanban-<role>")
                print(f"List all sessions:      tmux list-sessions")
        else:
            print("\nNo role sessions configured. Run 'kanban_cli init' for setup.")

        monitor_thread = threading.Thread(target=supervisor.wait, daemon=True)
        monitor_thread.start()
    else:
        print("\nSupervisor skipped (--no-supervisor).")

    print(f"\n{'=' * 60}")
    print(f"Kanban runtime is ready.")
    print(f"  UI: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop.")
    print(f"{'=' * 60}")

    try:
        server_proc.wait()
    except KeyboardInterrupt:
        _shutdown()


def cmd_roles_list(args):
    prefs = load_preferences()
    if not prefs:
        print("No preferences found. Run: python -m kanban_cli init")
        return

    assignments = prefs.get_role_assignments()
    adapters = {a.name: a for a in load_all_adapters()}

    print(f"{'Role':<16} {'Agent':<16} {'Display Name':<25} {'Mode':<12} {'Model':<24} {'Active'}")
    print("-" * 110)

    if not assignments:
        print("No role assignments configured.")
        return

    for role_name, assignment in assignments.items():
        adapter = adapters.get(assignment.agent)
        command = adapter.invoke.command if adapter else (assignment.command or assignment.agent)
        display = adapter.display_name if adapter else (assignment.display_name or assignment.agent)
        default_model = adapter.models[0].id if adapter and adapter.models else "default"
        model = assignment.model or default_model
        available = shutil.which(command) is not None
        status = "yes" if available else "no"
        print(f"{role_name:<16} {assignment.agent:<16} {display:<25} {assignment.mode:<12} {model:<24} {status}")


def cmd_roles_assign(args):
    prefs = load_preferences()
    if not prefs:
        prefs = Preferences(manager=ManagerConfig(agent=args.agent, model="default", mode="headless"))

    adapters = {a.name: a for a in load_all_adapters()}
    role_name = args.role
    valid_roles = [r.value for r in AgentRole]
    if role_name not in valid_roles:
        print(f"Invalid role '{role_name}'. Valid roles: {', '.join(valid_roles)}")
        return

    mode = args.mode or "headless"
    explicit_models = [m.strip() for m in (args.models or "").split(",") if m.strip()]
    selected_model = args.model
    adapter = adapters.get(args.agent)
    command = args.command
    if not adapter:
        command = command or args.agent
        if not shutil.which(command):
            print(
                f"Agent '{args.agent}' is not a configured adapter and CLI "
                f"'{command}' was not found on PATH."
            )
            print("Use 'python -m kanban_cli agents discover' to see common installed CLIs.")
            return
        assignment = RoleAssignment(
            agent=args.agent,
            mode=mode,
            model=selected_model or (explicit_models[0] if explicit_models else None),
            models=explicit_models,
            command=command,
            display_name=args.display_name or args.agent,
            protocol=args.protocol,
            capabilities=[role_name],
        )
    else:
        available_models = [m.id for m in adapter.models]
        if selected_model and available_models and selected_model not in available_models:
            print(f"Model '{selected_model}' is not in adapter model list: {', '.join(available_models)}")
            return
        assignment = RoleAssignment(
            agent=args.agent,
            mode=mode,
            model=selected_model or (available_models[0] if available_models else None),
            models=available_models,
        )

    if prefs.roles is None:
        prefs.roles = RoleConfig()

    setattr(prefs.roles, role_name, assignment)

    if role_name == "orchestrator" and prefs.manager is None:
        prefs.manager = ManagerConfig(
            agent=args.agent,
            model=assignment.model or "default",
            mode=mode,
        )

    save_preferences(prefs)
    display = adapter.display_name if adapter else (args.display_name or args.agent)
    source = "adapter" if adapter else f"standalone CLI: {command}"
    model_note = f", model: {assignment.model}" if assignment.model else ""
    print(f"Assigned '{display}' to role '{role_name}' (mode: {mode}{model_note}, {source})")


def cmd_sheet(args):
    """Print a clean local sheet of projects and agents."""
    import asyncio
    from sqlalchemy import select
    from database import async_session_maker
    from models import Entity, EntityType, Project, Task, task_assignments

    async def _run():
        import database
        database.engine.echo = False
        adapters = {a.name: a for a in load_all_adapters()}
        prefs = load_preferences()
        assignments = prefs.get_role_assignments() if prefs else {}
        role_agent_names = {assignment.agent for assignment in assignments.values()}
        adapter_names = set(adapters.keys())
        async with async_session_maker() as session:
            projects_result = await session.execute(select(Project).order_by(Project.id))
            projects = projects_result.scalars().all()
            agents_result = await session.execute(
                select(Entity).filter(Entity.entity_type == EntityType.AGENT).order_by(Entity.id)
            )
            agents = agents_result.scalars().all()
            task_result = await session.execute(select(Task))
            tasks = task_result.scalars().all()
            assignments_result = await session.execute(select(task_assignments))
            assignment_rows = assignments_result.fetchall()

        tasks_by_project = {}
        for task in tasks:
            tasks_by_project.setdefault(task.project_id, []).append(task)
        assigned_task_ids = {row.task_id for row in assignment_rows}

        def is_noisy_project(project):
            text = f"{project.name or ''} {project.path or ''}".lower()
            noisy_markers = [
                "test", "phase 6", "visibility", "coordination",
                "approval queue", "diff review", "reject project",
                "folder picker smoke", "/tmp/",
            ]
            return any(marker in text for marker in noisy_markers)

        def is_noisy_agent(agent):
            if agent.name in role_agent_names:
                return False
            if agent.name in adapter_names:
                return False
            text = (agent.name or "").lower()
            noisy_markers = ["test", "visibility", "heartbeat", "worker agent", "other agent", "coordination"]
            return any(marker in text for marker in noisy_markers)

        visible_projects = projects if args.all else [
            p for p in projects if not is_noisy_project(p) and p.approval_status.value != "REJECTED"
        ]
        visible_agents = agents if args.all else [
            a for a in agents if a.name in role_agent_names
        ]

        print("Projects")
        print(f"{'ID':<5} {'Status':<12} {'Tasks':<8} {'Assigned':<9} {'Path':<35} Name")
        print("-" * 100)
        for project in visible_projects:
            project_tasks = tasks_by_project.get(project.id, [])
            assigned_count = sum(1 for task in project_tasks if task.id in assigned_task_ids)
            print(
                f"{project.id:<5} {project.approval_status.value:<12} "
                f"{len(project_tasks):<8} {assigned_count:<9} "
                f"{(project.path or '-')[:35]:<35} {project.name}"
            )

        hidden_projects = len(projects) - len(visible_projects)
        if hidden_projects:
            print(f"... hidden {hidden_projects} test/demo projects. Use --all to show them.")

        print("\nAgents")
        print(f"{'ID':<5} {'Name':<18} {'Role':<10} {'Active':<8} {'Source':<12} Skills")
        print("-" * 105)
        for agent in visible_agents:
            source = "role" if agent.name in role_agent_names else ("adapter" if agent.name in adapter_names else "db")
            print(
                f"{agent.id:<5} {agent.name:<18} {agent.role.value:<10} "
                f"{'yes' if agent.is_active else 'no':<8} {source:<12} {agent.skills or '-'}"
            )
        hidden_agents = len(agents) - len(visible_agents)
        if hidden_agents:
            print(f"... hidden {hidden_agents} non-role/test/demo agents. Use --all to show them.")

        if assignments:
            print("\nRole Assignments")
            print(f"{'Role':<16} {'Agent':<18} {'Mode':<10} {'Model':<24} {'Available Models':<30} Source")
            print("-" * 120)
            for role_name, assignment in assignments.items():
                adapter = adapters.get(assignment.agent)
                adapter_models = [m.id for m in adapter.models] if adapter else []
                models = assignment.models or adapter_models
                model = assignment.model or (models[0] if models else "default")
                source = f"standalone:{assignment.command}" if assignment.command else "adapter"
                print(
                    f"{role_name:<16} {assignment.agent:<18} {assignment.mode:<10} "
                    f"{model:<24} {', '.join(models) or '-':<30} {source}"
                )

    asyncio.run(_run())


def cmd_roles_start(args):
    from kanban_runtime.role_supervisor import RoleSupervisor
    api_base = args.api_base or "http://localhost:8000"
    supervisor = RoleSupervisor(api_base=api_base)
    supervisor.start()

    if supervisor.sessions:
        print(f"Started {len(supervisor.sessions)} role sessions:")
        for role_name, session in supervisor.sessions.items():
            print(f"  {role_name}: {session.agent} ({session.tmux_session or f'PID {session.pid}'})")
    else:
        print("No sessions started. Check role assignments with: kanban roles list")

    print("Monitoring... Press Ctrl+C to stop.")
    try:
        supervisor.wait()
    except KeyboardInterrupt:
        supervisor.stop()


def cmd_roles_stop(args):
    from kanban_runtime.role_supervisor import RoleSupervisor
    api_base = args.api_base or "http://localhost:8000"
    supervisor = RoleSupervisor(api_base=api_base)

    if shutil.which("tmux"):
        import subprocess as sp
        result = sp.run(["tmux", "list-sessions"], capture_output=True, text=True)
        sessions = [line.split(":")[0] for line in result.stdout.strip().split("\n") if line]
        kanban_sessions = [s for s in sessions if s.startswith("kanban-")]
        for sn in kanban_sessions:
            sp.run(["tmux", "kill-session", "-t", sn], capture_output=True)
            print(f"Killed tmux session: {sn}")
        if not kanban_sessions:
            print("No kanban tmux sessions found.")
    else:
        print("tmux not available. Use Ctrl+C on the running process.")


def cmd_roles_status(args):
    from kanban_runtime.role_supervisor import RoleSupervisor
    api_base = args.api_base or "http://localhost:8000"
    supervisor = RoleSupervisor(api_base=api_base)

    prefs = load_preferences()
    if not prefs:
        print("No preferences found.")
        return

    assignments = prefs.get_role_assignments()
    adapters = {a.name: a for a in load_all_adapters()}

    print(f"{'Role':<16} {'Agent':<16} {'Tmux Session':<25} {'Alive'}")
    print("-" * 70)

    for role_name, assignment in assignments.items():
        session = supervisor.sessions.get(role_name)
        if session:
            alive = False
            if session.tmux_session:
                from kanban_runtime.role_supervisor import tmux_is_running
                alive = tmux_is_running(session.tmux_session)
            elif session.process:
                alive = session.process.poll() is None
            print(f"{role_name:<16} {session.agent:<16} {session.tmux_session or 'N/A':<25} {'yes' if alive else 'no'}")
        else:
            adapter = adapters.get(assignment.agent)
            print(f"{role_name:<16} {assignment.agent:<16} {'not started':<25} unknown")


def main():
    parser = argparse.ArgumentParser(prog="kanban", description="Agent Kanban PM CLI")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Run setup wizard")
    init_parser.set_defaults(func=cmd_init)

    agents_parser = subparsers.add_parser("agents", help="Manage agents")
    agents_sub = agents_parser.add_subparsers(dest="agents_cmd")
    agents_sub.add_parser("list", help="List installed adapters").set_defaults(func=cmd_agents_list)
    agents_sub.add_parser("discover", help="Check popular CLI tools without registering them").set_defaults(func=cmd_agents_discover)

    daemon_parser = subparsers.add_parser("daemon", help="Manage manager daemon")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_cmd")
    daemon_sub.add_parser("start", help="Start manager daemon").set_defaults(func=cmd_daemon)
    daemon_sub.add_parser("status", help="Check daemon status").set_defaults(func=cmd_daemon_status)
    daemon_sub.add_parser("stop", help="Stop manager daemon").set_defaults(func=cmd_daemon_stop)
    daemon_parser.set_defaults(func=cmd_daemon)

    run_parser = subparsers.add_parser("run", help="Start server + UI + role supervisor")
    run_parser.add_argument("--host", default="0.0.0.0", help="Server host")
    run_parser.add_argument("--port", type=int, default=8000, help="Server port")
    run_parser.add_argument("--api-base", default="http://localhost:8000", help="API base URL")
    run_parser.add_argument("--no-supervisor", action="store_true", help="Skip role supervisor")
    run_parser.set_defaults(func=cmd_run)

    roles_parser = subparsers.add_parser("roles", help="Manage role assignments")
    roles_sub = roles_parser.add_subparsers(dest="roles_cmd")

    roles_sub.add_parser("list", help="List role assignments").set_defaults(func=cmd_roles_list)

    roles_assign = roles_sub.add_parser("assign", help="Assign agent to role")
    roles_assign.add_argument("role", help="Role name (orchestrator, ui, architecture, worker, test, diff_review, git_pr)")
    roles_assign.add_argument("agent", help="Agent adapter name")
    roles_assign.add_argument("--mode", default="headless", help="Agent mode (supervised/auto/headless)")
    roles_assign.add_argument("--model", help="Default model for this role")
    roles_assign.add_argument("--models", help="Comma-separated allowed model list for standalone CLI roles")
    roles_assign.add_argument("--command", help="Standalone CLI command to run when agent is not an adapter")
    roles_assign.add_argument("--display-name", help="Display name for a standalone CLI role")
    roles_assign.add_argument("--protocol", default="stdio", help="Protocol label for standalone CLI roles")
    roles_assign.set_defaults(func=cmd_roles_assign)

    roles_start = roles_sub.add_parser("start", help="Start all role agents")
    roles_start.add_argument("--api-base", default="http://localhost:8000", help="API base URL")
    roles_start.set_defaults(func=cmd_roles_start)
    roles_sub.add_parser("stop", help="Stop all role agents").set_defaults(func=cmd_roles_stop)
    roles_status = roles_sub.add_parser("status", help="Show role session status")
    roles_status.add_argument("--api-base", default="http://localhost:8000", help="API base URL")
    roles_status.set_defaults(func=cmd_roles_status)

    sheet_parser = subparsers.add_parser("sheet", help="Print a clean sheet of projects, agents, and role assignments")
    sheet_parser.add_argument("--all", action="store_true", help="Include test/demo database rows")
    sheet_parser.set_defaults(func=cmd_sheet)

    chat_parser = subparsers.add_parser(
        "chat",
        help="Interactive REPL: chat with the orchestrator to design backlog cards (AGENTS.md §10)",
    )
    chat_parser.add_argument("project_id", type=int, help="Project ID to add tasks to")
    chat_parser.add_argument(
        "--api-base", default=None,
        help="API base URL (default: $KANBAN_API_BASE or http://localhost:8000)",
    )
    chat_parser.add_argument(
        "--entity-id", type=int, default=None,
        help="X-Entity-ID for the human caller (default: $KANBAN_ENTITY_ID or server fallback)",
    )

    def _cmd_chat(args):
        from kanban_cli.chat import cmd_chat as _run
        _run(args)

    chat_parser.set_defaults(func=_cmd_chat)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
