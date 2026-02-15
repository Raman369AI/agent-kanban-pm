---
description: Sync agent work stages with the Kanban board at http://localhost:8000
---

# Kanban Auto-Sync Workflow

This workflow ensures that all agent work is tracked on the Agent Kanban PM board.
The server must be running at `http://localhost:8000`.

## Agent Credentials

- **Agent Name**: Antigravity
- **Agent ID**: 1
- **API Key**: `XnLxi3P69MeBOT3tzGmJ2HRgr2fBRQV-2bfwpGObtZw`

All API calls use the header: `-H "X-API-Key: XnLxi3P69MeBOT3tzGmJ2HRgr2fBRQV-2bfwpGObtZw"`

## Stage IDs (Project 1)

| Stage       | ID |
|-------------|-----|
| Backlog     | 1   |
| To Do       | 2   |
| In Progress | 3   |
| Review      | 4   |
| Done        | 5   |

## Workflow Steps

### 1. At the Start of Any Task

When the user gives you a task, check if a matching task already exists on the board:

// turbo
```bash
curl -s "http://localhost:8000/tasks?project_id=1" \
  -H "X-API-Key: XnLxi3P69MeBOT3tzGmJ2HRgr2fBRQV-2bfwpGObtZw" | python3 -m json.tool
```

If no matching task exists, create one in the **Backlog** stage:

```bash
curl -s -X POST "http://localhost:8000/tasks" \
  -H "X-API-Key: XnLxi3P69MeBOT3tzGmJ2HRgr2fBRQV-2bfwpGObtZw" \
  -H "Content-Type: application/json" \
  -d '{"title":"TASK_TITLE","description":"TASK_DESCRIPTION","project_id":PROJECT_ID,"stage_id":1,"required_skills":"SKILLS","priority":PRIORITY}'
```

Self-assign the task:

```bash
curl -s -X POST "http://localhost:8000/tasks/TASK_ID/self-assign" \
  -H "X-API-Key: XnLxi3P69MeBOT3tzGmJ2HRgr2fBRQV-2bfwpGObtZw"
```

### 2. When Starting Work (PLANNING → EXECUTION)

Move task to **In Progress** (stage_id=3) and update status:

```bash
curl -s -X PATCH "http://localhost:8000/tasks/TASK_ID" \
  -H "X-API-Key: XnLxi3P69MeBOT3tzGmJ2HRgr2fBRQV-2bfwpGObtZw" \
  -H "Content-Type: application/json" \
  -d '{"stage_id":3,"status":"in_progress"}'
```

### 3. When Work Is Complete (Ready for Review)

Move task to **Review** (stage_id=4) and update status:

```bash
curl -s -X PATCH "http://localhost:8000/tasks/TASK_ID" \
  -H "X-API-Key: XnLxi3P69MeBOT3tzGmJ2HRgr2fBRQV-2bfwpGObtZw" \
  -H "Content-Type: application/json" \
  -d '{"stage_id":4,"status":"in_review"}'
```

At this point, **wait for the user**. The user will review the work on the Kanban board:
- If the user **moves the task to Done** → the work is approved
- If the user **moves it back** to In Progress or To Do → changes are needed

### 4. Adding Progress Comments

Log significant progress updates as comments:

```bash
curl -s -X POST "http://localhost:8000/comments" \
  -H "X-API-Key: XnLxi3P69MeBOT3tzGmJ2HRgr2fBRQV-2bfwpGObtZw" \
  -H "Content-Type: application/json" \
  -d '{"task_id":TASK_ID,"content":"PROGRESS_UPDATE"}'
```

## Stage Flow

```
Backlog → To Do → In Progress → Review → Done
  (1)      (2)       (3)         (4)     (5)
   └── agent creates task here
                      └── agent moves task here when starting work
                                      └── agent moves task here when done
                                                    └── USER moves task here to approve
```

## Notes

- Always check for existing tasks before creating new ones
- Use priority 1-10 (10 = highest)
- The user approves by moving tasks from Review → Done on the dashboard
- If the user moves a task back, re-work it and move to Review again
