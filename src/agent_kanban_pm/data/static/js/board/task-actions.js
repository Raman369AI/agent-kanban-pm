    // --- Move a Backlog card to To Do ---
    window.moveToTodo = function(taskId, btn) {
        var todoCol = document.querySelector('.kanban-column-revamp[data-stage-key="to_do"]');
        if (!todoCol) { showToast('No "To Do" column found', 'error'); return; }
        var todoZone = todoCol.querySelector('.kanban-drop-zone-revamp');
        var todoStageId = todoZone.dataset.stageId;
        var card = document.getElementById('task-card-' + taskId);
        if (!card) { showToast('Task card not found', 'error'); return; }
        if (card.dataset.currentStage === todoStageId) return;
        if (btn) btn.disabled = true;
        moveTaskCard(card, todoZone).then(function(moved) {
            if (!moved) return null;
            return refreshBoardFromServer().then(function() {
                showToast('Task is ready in To Do. Assign a worker to start it.', 'success');
            });
        }).finally(function() {
            if (btn && btn.isConnected) btn.disabled = false;
        });
    };

    // --- Start an unambiguous assigned Backlog task ---
    window.startTask = function(taskId, btn, startState) {
        var card = document.getElementById('task-card-' + taskId);
        startState = startState || (card && card.dataset.startState) || 'needs_assignment';
        if (startState !== 'eligible') {
            showToast(
                startState === 'choose_agent'
                    ? 'Choose one active agent before starting this task.'
                    : 'Assign an active agent before starting this task.',
                'info'
            );
            openAssignModal(taskId);
            return;
        }
        var todoCol = document.querySelector('.kanban-column-revamp[data-stage-key="to_do"]');
        if (!todoCol) { showToast('No "To Do" column found', 'error'); return; }
        if (!card) { showToast('Task card not found', 'error'); return; }
        var todoZone = todoCol.querySelector('.kanban-drop-zone-revamp');
        if (btn) {
            btn.disabled = true;
            btn.dataset.originalText = btn.textContent;
            btn.textContent = 'Starting\u2026';
        }
        moveTaskCard(card, todoZone).then(function(moved) {
            if (!moved) return null;
            var agentName = card.dataset.startAgentName || 'assigned agent';
            return refreshBoardFromServer().then(function() {
                showToast('Task queued for ' + agentName + '.', 'success');
            });
        }).finally(function() {
            if (btn && btn.isConnected) {
                btn.disabled = false;
                btn.textContent = btn.dataset.originalText || 'Start';
            }
        });
    };

    window.requestGitPr = function(taskId, btn) {
        if (!btn || btn.disabled) return;
        var originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Requesting\u2026';
        apiFetch('/ui/tasks/' + taskId + '/request-git-pr', {
            method: 'POST',
            headers: {'x-entity-id': CURRENT_ENTITY_ID}
        }, 'Failed to request Git/PR handoff')
            .then(function(result) {
                var message = result.created
                    ? 'Git/PR handoff queued.'
                    : result.state === 'completed'
                        ? 'Git/PR handoff is already complete.'
                        : 'Git/PR handoff is already ' + String(result.state || 'queued').replace(/_/g, ' ') + '.';
                showToast(message, result.created ? 'success' : 'info');
                return refreshBoardFromServer();
            })
            .catch(function(err) {
                showToast('Git/PR request failed: ' + err.message, 'error');
                if (btn.isConnected) {
                    btn.disabled = false;
                    btn.textContent = originalText;
                }
            });
    };

    // --- Task CRUD ---
    function setTaskFormError(message) {
        var el = document.getElementById('task-form-error');
        if (!el) return;
        el.textContent = message || '';
        el.style.display = message ? '' : 'none';
    }
    function setPriorityValue(value) {
        var select = document.getElementById('task-form-priority');
        select.querySelectorAll('option[data-preserved="true"]').forEach(function(option) { option.remove(); });
        var key = String(value === null || value === undefined ? 0 : value);
        if (!Array.from(select.options).some(function(option) { return option.value === key; })) {
            var option = new Option('Current: ' + priorityName(key) + ' (' + key + ')', key);
            option.dataset.preserved = 'true';
            select.add(option);
        }
        select.value = key;
    }
    var editDetailRequestSerial = 0;
    window.openAddTaskModal = function(stageId, stageName) {
        document.getElementById('task-modal-title').textContent = 'New task';
        document.getElementById('task-form-mode').value = 'create';
        document.getElementById('task-form-id').value = '';
        document.getElementById('task-form-version').value = '';
        document.getElementById('task-form-submit').disabled = false;
        var stageSelect = document.getElementById('task-form-stage');
        if (stageId) stageSelect.value = String(stageId);
        else if (PLAN_STAGE_ID) stageSelect.value = String(PLAN_STAGE_ID);
        else stageSelect.selectedIndex = 0;
        stageSelect.disabled = false;
        document.getElementById('task-form-stage-hint').style.display = 'none';
        document.getElementById('task-form-title').value = '';
        document.getElementById('task-form-desc').value = '';
        setPriorityValue(0);
        document.getElementById('task-form-status').value = 'pending';
        document.getElementById('task-form-skills').value = '';
        document.getElementById('task-form-submit').textContent = 'Create task';
        document.getElementById('task-comments-section').style.display = 'none';
        setTaskFormError('');
        openModal('task-modal');
    };
    // Header entry point: same dialog, destination stage preselected.
    window.openNewTaskModal = function() { window.openAddTaskModal(null, null); };
    window.openEditModal = function(taskEl) {
        var taskId = taskEl.dataset.taskId;
        document.getElementById('task-modal-title').textContent = 'Edit Task #' + taskId;
        document.getElementById('task-form-mode').value = 'edit';
        document.getElementById('task-form-id').value = taskId;
        document.getElementById('task-form-version').value = '';
        document.getElementById('task-form-submit').disabled = true;
        var requestSerial = ++editDetailRequestSerial;
        var stageSelect = document.getElementById('task-form-stage');
        if (taskEl.dataset.currentStage) stageSelect.value = taskEl.dataset.currentStage;
        // The edit endpoint does not move cards; the board does.
        stageSelect.disabled = true;
        document.getElementById('task-form-stage-hint').style.display = '';
        var title = taskEl.querySelector('.task-title-revamp');
        var initialTitle = title ? title.textContent.trim() : '';
        document.getElementById('task-form-title').value = initialTitle;
        document.getElementById('task-form-desc').value = '';
        document.getElementById('task-form-status').value = 'pending';
        setPriorityValue(0);
        document.getElementById('task-form-skills').value = '';
        document.getElementById('task-form-submit').textContent = 'Save changes';
        setTaskFormError('');
        var commentsList = document.getElementById('task-comments-list');
        commentsList.innerHTML = '<p class="text-secondary">Loading activity...</p>';
        document.getElementById('task-comments-section').style.display = 'block';
        apiFetch('/tasks/' + taskId, {}, 'Failed to load task details').then(function(data) {
            if (requestSerial !== editDetailRequestSerial ||
                document.getElementById('task-form-id').value !== String(taskId)) return;
            document.getElementById('task-form-version').value = String(data.version);
            document.getElementById('task-form-submit').disabled = false;
            // A slow detail response must not replace text typed while it loaded.
            var titleInput = document.getElementById('task-form-title');
            var descInput = document.getElementById('task-form-desc');
            var statusInput = document.getElementById('task-form-status');
            var priorityInput = document.getElementById('task-form-priority');
            var skillsInput = document.getElementById('task-form-skills');
            if (titleInput.value === initialTitle) titleInput.value = data.title || '';
            if (descInput.value === '') descInput.value = data.description || '';
            if (statusInput.value === 'pending') statusInput.value = data.status || 'pending';
            if (data.priority !== undefined && priorityInput.value === '0') setPriorityValue(data.priority);
            if (skillsInput.value === '') skillsInput.value = data.required_skills || '';
            var html = '';
            if (data.comments && data.comments.length > 0) {
                data.comments.forEach(function(c) {
                    var author = c.author ? c.author.name : 'Unknown';
                    var date = new Date(c.created_at).toLocaleString();
                    var icon = c.author && c.author.entity_type === 'agent' ? '\u{1F916}' : '\u{1F464}';
                    html += '<div style="margin-bottom: 0.5rem; padding: 0.5rem; background: var(--bg-secondary); border-radius: 4px;"><div style="font-size: 0.8rem; color: var(--text-secondary); display: flex; justify-content: space-between;"><span>' + icon + ' <strong>' + escapeHtml(author) + '</strong></span><span>' + escapeHtml(date) + '</span></div><div style="margin-top: 0.25rem;">' + escapeHtml(c.content) + '</div></div>';
                });
            } else { html = '<p class="text-secondary">No comments yet.</p>'; }
            commentsList.innerHTML = html;
        }).catch(function(err) {
            if (requestSerial !== editDetailRequestSerial) return;
            commentsList.innerHTML = '<p class="text-danger">' + escapeHtml(err.message) + '</p>';
            setTaskFormError('Could not load task details. Close and try again.');
            showToast('Could not load task: ' + err.message, 'error');
        });
        openModal('task-modal');
    };
    var boardRefreshInFlight = null;
    var boardRefreshPending = false;
    function refreshBoardFromServer() {
        if (boardRefreshInFlight) {
            boardRefreshPending = true;
            return boardRefreshInFlight;
        }
        if (draggedTask || document.querySelector('[data-moving="true"]')) {
            clearTimeout(refreshBoardFromServer.retry);
            refreshBoardFromServer.retry = setTimeout(refreshBoardFromServer, 250);
            return Promise.resolve(false);
        }
        var board = document.getElementById('board-main-revamp');
        if (!board) return Promise.resolve(false);
        boardRefreshInFlight = apiFetch(
            window.location.href,
            { headers: { 'X-Requested-With': 'fetch' } },
            'Refresh failed'
        )
            .then(function(html) {
                var doc = new DOMParser().parseFromString(html, 'text/html');
                var freshBoard = doc.getElementById('board-main-revamp');
                if (!freshBoard) throw new Error('Board markup missing');
                if (draggedTask || document.querySelector('[data-moving="true"]')) {
                    boardRefreshPending = true;
                    return false;
                }
                var active = document.activeElement;
                var activeCard = active && active.closest('.kanban-task-revamp');
                var focusIndex = activeCard ? Array.from(activeCard.querySelectorAll('button, [tabindex]')).indexOf(active) : -1;
                var scrollPositions = Array.from(board.querySelectorAll('.kanban-container-revamp, .kanban-drop-zone-revamp'))
                    .map(function(el) { return {stage: el.dataset.stageId, top: el.scrollTop, left: el.scrollLeft}; });
                returnPanelContentToCard();
                board.replaceWith(freshBoard);
                var checklist = document.getElementById('project-setup-checklist');
                var freshChecklist = doc.getElementById('project-setup-checklist');
                if (checklist && freshChecklist) {
                    var oldGrid = checklist.querySelector('#setup-steps-grid');
                    var nextGrid = freshChecklist.querySelector('#setup-steps-grid');
                    if (oldGrid && nextGrid && oldGrid.style.display === 'none') {
                        nextGrid.style.display = 'none';
                        var nextIcon = freshChecklist.querySelector('#checklist-toggle-icon');
                        if (nextIcon) nextIcon.innerHTML = '&#9660;';
                    }
                    checklist.replaceWith(freshChecklist);
                }
                initDragDrop();
                initExpandedCards();
                loadReviewGateBadges();
                updateAgentFilterChoices();
                applyBoardFilters(true);
                fetchAgentApprovals();
                if (activeCard) {
                    var replacement = document.getElementById(activeCard.id);
                    var focusTarget = replacement && (focusIndex < 0 ? replacement : replacement.querySelectorAll('button, [tabindex]')[focusIndex]);
                    if (focusTarget) focusTarget.focus({preventScroll: true});
                }
                scrollPositions.forEach(function(position) {
                    var el = position.stage ? freshBoard.querySelector('.kanban-drop-zone-revamp[data-stage-id="' + position.stage + '"]') : freshBoard.querySelector('.kanban-container-revamp');
                    if (el) { el.scrollTop = position.top; el.scrollLeft = position.left; }
                });
                if (boardSocket && boardSocket.readyState === WebSocket.OPEN) setBoardConnectionStatus('Live', false);
                return true;
            }).catch(function(error) {
                if (boardSocket && boardSocket.readyState === WebSocket.OPEN) setBoardConnectionStatus('Refresh failed', true);
                showToast('Could not refresh board: ' + error.message, 'error');
                return false;
            }).finally(function() {
                boardRefreshInFlight = null;
                if (boardRefreshPending) {
                    boardRefreshPending = false;
                    refreshBoardFromServer();
                }
            });
        return boardRefreshInFlight;
    }
    window.refreshBoardFromServer = refreshBoardFromServer;
    var taskFormPending = false;
    window.submitTaskForm = function(btn) {
        if (taskFormPending) return;
        var mode = document.getElementById('task-form-mode').value;
        var title = document.getElementById('task-form-title').value.trim();
        if (!title) { showToast('Title is required', 'error'); return; }
        var data = { title: title, description: document.getElementById('task-form-desc').value.trim(), priority: parseInt(document.getElementById('task-form-priority').value) || 0, status: document.getElementById('task-form-status').value, required_skills: document.getElementById('task-form-skills').value.trim() };
        taskFormPending = true;
        if (btn) { btn.disabled = true; }
        setTaskFormError('');
        var request;
        if (mode === 'create') {
            data.project_id = PROJECT_ID;
            data.stage_id = parseInt(document.getElementById('task-form-stage').value);
            request = apiFetch('/ui/tasks/create', { method: 'POST', headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID }, body: JSON.stringify(data) }, 'Failed to create task')
                .then(function() { showToast('Task created', 'success'); closeModal('task-modal'); return refreshBoardFromServer(); })
                .catch(function(err) { setTaskFormError(err.message); showToast('Error: ' + err.message, 'error'); });
        } else {
            var taskId = document.getElementById('task-form-id').value;
            var version = document.getElementById('task-form-version').value;
            if (!version) {
                taskFormPending = false;
                if (btn) btn.disabled = false;
                setTaskFormError('Task details are still loading. Try again.');
                return;
            }
            data.version = Number(version);
            function saveEdit(reason) {
                data.override_reason = reason || '';
                return apiFetch('/ui/tasks/' + taskId + '/edit', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID },
                    body: JSON.stringify(data)
                }, 'Failed to update task');
            }
            function finishEdit() {
                showToast('Task updated!', 'success');
                closeModal('task-modal');
                return refreshBoardFromServer();
            }
            function editError(err) {
                setTaskFormError(err.message);
                showToast('Error: ' + err.message, 'error');
                return false;
            }
            request = saveEdit('').then(finishEdit).catch(function(err) {
                if (err.message.toLowerCase().indexOf('override reason is required') === -1) {
                    return editError(err);
                }
                var decisionRequest = data.status === 'completed'
                    ? requestDoneTransition(taskId, 'task-modal')
                    : requestPolicyOverride({
                        parentModalId: 'task-modal',
                        title: 'Override workflow gate?',
                        actionLabel: 'Save transition anyway',
                        blocker: err.message.replace(/\.? A human override reason is required to continue$/i, ''),
                        evidenceLabel: 'This task update is blocked by:',
                        gates: []
                    });
                return decisionRequest.then(function(decision) {
                    if (!decision || !decision.proceed) {
                        setTaskFormError('The task was not changed.');
                        return false;
                    }
                    return saveEdit(decision.reason).then(finishEdit).catch(editError);
                });
            });
        }
        request.finally(function() {
            taskFormPending = false;
            if (btn) { btn.disabled = false; }
        });
    };
    window.deleteTask = async function(taskId, taskEl) {
        var confirmed = await confirmAction({
            title: 'Delete task?',
            message: 'This permanently deletes the task and its recorded work history.',
            confirmLabel: 'Delete task',
            danger: true
        });
        if (!confirmed) return;
        var stageId = taskEl.dataset.currentStage;
        apiFetch('/ui/tasks/' + taskId, { method: 'DELETE', headers: { 'x-entity-id': CURRENT_ENTITY_ID } }, 'Failed to delete task')
            .then(function() { if (activeTaskId === Number(taskId)) closeTaskPanel(); var dropZone = taskEl.parentElement; taskEl.remove(); addPlaceholderIfEmpty(dropZone); updateColumnCount(stageId, -1); showToast('Task deleted', 'success'); })
            .catch(function(err) { showToast('Error: ' + err.message, 'error'); });
    };
