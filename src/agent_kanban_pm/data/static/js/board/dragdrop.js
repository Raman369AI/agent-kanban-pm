    // --- Drag and Drop ---
    function expectLocalMove(taskId, stageId) {
        var key = String(taskId);
        var expectedStage = String(stageId);
        expectedLocalMoves[key] = expectedStage;
        setTimeout(function() {
            if (expectedLocalMoves[key] === expectedStage) delete expectedLocalMoves[key];
        }, 2000);
    }
    function forgetLocalMove(taskId, stageId) {
        var key = String(taskId);
        if (expectedLocalMoves[key] === String(stageId)) delete expectedLocalMoves[key];
    }
    function consumeLocalMoveEvent(data) {
        var key = String(data.task_id);
        return expectedLocalMoves[key] === String(data.to_stage_id);
    }
    function initDragDrop() {
        document.querySelectorAll('.kanban-task-revamp').forEach(function(task) { bindDragEvents(task); });
        document.querySelectorAll('.kanban-drop-zone-revamp').forEach(function(zone) { bindDropZone(zone); });
    }
    function bindDragEvents(task) {
        task.addEventListener('dragstart', function(e) {
            draggedTask = this;
            this.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', this.dataset.taskId);
            var self = this;
            setTimeout(function() { if (draggedTask) draggedTask.style.opacity = '0.4'; }, 0);
        });
        task.addEventListener('dragend', function() {
            this.classList.remove('dragging');
            this.style.opacity = '';
            document.querySelectorAll('.kanban-drop-zone-revamp').forEach(function(zone) { zone.classList.remove('drag-over'); });
            draggedTask = null;
        });
    }
    var pendingDoneOverrideResolve = null;
    var pendingDoneOverrideParent = null;

    function restoreDoneOverrideParent() {
        var parent = pendingDoneOverrideParent;
        pendingDoneOverrideParent = null;
        if (!parent) return;
        parent.classList.remove('modal-suspended');
        parent.setAttribute('aria-hidden', 'false');
    }

    function closeDoneOverrideModal(result) {
        restoreDoneOverrideParent();
        closeModal('done-override-modal');
        var resolve = pendingDoneOverrideResolve;
        pendingDoneOverrideResolve = null;
        if (resolve) resolve(result);
    }

    function cancelDoneOverride() {
        closeDoneOverrideModal(null);
    }

    function submitDoneOverride() {
        var reason = document.getElementById('done-override-reason').value.trim();
        var error = document.getElementById('done-override-error');
        if (reason.length < 3) {
            error.textContent = 'Enter a short reason for overriding the workflow gate.';
            error.style.display = 'block';
            return;
        }
        closeDoneOverrideModal({proceed: true, reason: reason});
    }

    function requestPolicyOverride(options) {
        options = options || {};
        var parent = options.parentModalId && document.getElementById(options.parentModalId);
        if (parent && parent.style.display !== 'none') {
            pendingDoneOverrideParent = parent;
            parent.classList.add('modal-suspended');
            parent.setAttribute('aria-hidden', 'true');
        }
        document.getElementById('done-override-title').textContent =
            options.title || 'Override workflow gate?';
        document.getElementById('done-override-submit').textContent =
            options.actionLabel || 'Continue anyway';
        document.getElementById('done-override-blocker').textContent =
            options.blocker || 'Required workflow evidence is missing.';
        document.getElementById('done-override-evidence-label').textContent =
            options.evidenceLabel || 'The following required evidence is still missing:';
        var list = document.getElementById('done-override-gates');
        list.replaceChildren();
        (options.gates || []).forEach(function(gate) {
            var item = document.createElement('li');
            var title = document.createElement('strong');
            title.textContent = gate.label || 'Workflow gate';
            var detail = document.createElement('span');
            detail.textContent = gate.detail || '';
            item.append(title, detail);
            list.append(item);
        });
        if (!list.children.length) {
            var item = document.createElement('li');
            var title = document.createElement('strong');
            title.textContent = 'Policy evidence';
            var detail = document.createElement('span');
            detail.textContent = options.blocker || 'This transition does not meet the configured workflow policy.';
            item.append(title, detail);
            list.append(item);
        }
        document.getElementById('done-override-reason').value = '';
        document.getElementById('done-override-error').style.display = 'none';
        openModal('done-override-modal');
        document.getElementById('done-override-reason').focus();
        return new Promise(function(resolve) {
            pendingDoneOverrideResolve = resolve;
        });
    }

    function requestDoneTransition(taskId, parentModalId) {
        return apiFetch(
            '/ui/tasks/' + taskId + '/completion-gates',
            {},
            'Could not check completion gates'
        ).then(function(completion) {
            updateReviewGateBadge(taskId, completion);
            if (completion.can_complete_automatically) {
                return {proceed: true, reason: ''};
            }
            return requestPolicyOverride({
                parentModalId: parentModalId,
                title: 'Move to Done anyway?',
                actionLabel: 'Move to Done anyway',
                blocker: completion.blocker || 'Required completion evidence is missing.',
                gates: (completion.gates || []).filter(function(gate) {
                    return gate.required && gate.state !== 'complete';
                })
            });
        }).catch(function(err) {
            showToast('Could not check completion gates: ' + err.message, 'error');
            return null;
        });
    }

    function moveTaskCard(card, targetZone, moveOptions) {
        if (!card || !targetZone || card.dataset.moving === 'true') return Promise.resolve(false);
        var taskId = card.dataset.taskId;
        var oldStageId = card.dataset.currentStage;
        var newStageId = targetZone.dataset.stageId;
        if (oldStageId === newStageId) return Promise.resolve(false);
        var column = targetZone.closest('.kanban-column-revamp');
        moveOptions = moveOptions || {};
        if (column.dataset.stageKey === 'done' && !moveOptions.preflighted) {
            return requestDoneTransition(taskId).then(function(decision) {
                if (!decision || !decision.proceed) return false;
                return moveTaskCard(card, targetZone, {
                    preflighted: true,
                    overrideReason: decision.reason || ''
                });
            });
        }
        var stageName = column.dataset.stageName;
        var newStatus = column.dataset.stageStatus || card.dataset.status;
        var oldStatus = card.dataset.status;
        var oldZone = card.parentElement;
        var statusBadge = card.querySelector('[id^="status-task-"]');
        var oldStatusText = statusBadge ? statusBadge.textContent : '';
        var oldStatusClass = statusBadge ? statusBadge.className : '';
        card.dataset.moving = 'true';
        clearPlaceholder(targetZone);
        targetZone.appendChild(card);
        card.dataset.currentStage = newStageId;
        card.dataset.status = newStatus;
        card.style.opacity = '';
        if (statusBadge) {
            statusBadge.textContent = statusLabel(newStatus);
            statusBadge.className = 'badge badge-' + newStatus;
        }
        addPlaceholderIfEmpty(oldZone);
        updateColumnCount(oldStageId, -1);
        updateColumnCount(newStageId, 1);
        expectLocalMove(taskId, newStageId);
        return apiFetch('/ui/tasks/' + taskId + '/move', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID },
            body: JSON.stringify({
                stage_id: parseInt(newStageId),
                status: newStatus,
                summary: moveOptions.overrideReason || '',
                override_reason: moveOptions.overrideReason || ''
            })
        }, 'Failed to update task').then(function() {
            card.setAttribute('aria-label', 'Task ' +
                (card.querySelector('.task-title-revamp') || {}).textContent + ' in ' + stageName +
                '. Use left and right arrow keys to move.');
            showToast('Task moved to ' + stageName, 'success');
            if (activeTaskId === Number(taskId)) fetchTaskOverview(Number(taskId));
            applyBoardFilters(true);
            return true;
        }).catch(function(err) {
            forgetLocalMove(taskId, newStageId);
            clearPlaceholder(oldZone);
            oldZone.appendChild(card);
            card.dataset.currentStage = oldStageId;
            card.dataset.status = oldStatus;
            if (statusBadge) {
                statusBadge.textContent = oldStatusText;
                statusBadge.className = oldStatusClass;
            }
            addPlaceholderIfEmpty(targetZone);
            updateColumnCount(oldStageId, 1);
            updateColumnCount(newStageId, -1);
            if (!moveOptions.overrideReason &&
                    err.message.toLowerCase().indexOf('override reason is required') !== -1) {
                return requestPolicyOverride({
                    title: 'Override workflow gate?',
                    actionLabel: 'Move to ' + stageName + ' anyway',
                    blocker: err.message.replace(/\.? A human override reason is required to continue$/i, ''),
                    evidenceLabel: 'This transition is blocked by:',
                    gates: []
                }).then(function(decision) {
                    if (!decision || !decision.proceed) return false;
                    delete card.dataset.moving;
                    return moveTaskCard(card, targetZone, {
                        preflighted: true,
                        overrideReason: decision.reason
                    });
                });
            }
            showToast('Failed to move task: ' + err.message, 'error');
            return false;
        }).finally(function() {
            delete card.dataset.moving;
            if (!activeTaskId) card.focus();
        });
    }
    window.handleTaskKeydown = function(event, card) {
        if (event.target !== card) return;
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            toggleCardExpansion(parseInt(card.dataset.taskId));
            return;
        }
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        event.preventDefault();
        var columns = Array.from(document.querySelectorAll('.kanban-column-revamp'));
        var currentColumn = card.closest('.kanban-column-revamp');
        var currentIndex = columns.indexOf(currentColumn);
        var nextIndex = currentIndex + (event.key === 'ArrowRight' ? 1 : -1);
        if (nextIndex < 0 || nextIndex >= columns.length) {
            showToast('Task is already at the edge of the board', 'info');
            return;
        }
        moveTaskCard(card, columns[nextIndex].querySelector('.kanban-drop-zone-revamp'));
    };
    function bindDropZone(zone) {
        zone.addEventListener('dragover', function(e) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; this.classList.add('drag-over'); });
        zone.addEventListener('dragleave', function(e) { if (!this.contains(e.relatedTarget)) this.classList.remove('drag-over'); });
        zone.addEventListener('drop', function(e) {
            e.preventDefault();
            this.classList.remove('drag-over');
            if (draggedTask) moveTaskCard(draggedTask, this);
        });
    }
