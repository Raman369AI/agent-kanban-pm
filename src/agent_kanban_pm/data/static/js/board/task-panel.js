    // --- One task detail panel, with the existing secondary views ---
    var activeTaskId = null;
    var panelReturnFocus = null;
    var panelHistoryEntry = false;
    var panelLoadSerial = 0;
    var boardTerminalRefreshTimer = null;

    function getExpandedCardTabs() {
        try {
            var stored = localStorage.getItem('kanban.expandedTabs.' + PROJECT_ID);
            return stored ? JSON.parse(stored) : {};
        } catch(e) { return {}; }
    }
    function saveExpandedCardTabs() {
        try { localStorage.setItem('kanban.expandedTabs.' + PROJECT_ID, JSON.stringify(expandedCardTabs)); } catch(e) {}
    }
    function taskIdFromUrl() {
        var value = new URL(window.location.href).searchParams.get('task');
        var id = Number(value);
        return Number.isInteger(id) && id > 0 ? id : null;
    }
    function returnPanelContentToCard() {
        var content = document.getElementById('task-panel-content');
        var expansion = content && content.querySelector('.task-expansion-panel');
        if (!expansion) return;
        var taskId = Number(expansion.id.replace('task-expansion-', ''));
        var card = document.getElementById('task-card-' + taskId);
        expansion.style.display = 'none';
        if (card) card.insertBefore(expansion, card.querySelector('.task-footer-revamp'));
        else expansion.remove();
    }
    function attachTaskPanel(taskId, reloadData) {
        var card = document.getElementById('task-card-' + taskId);
        var expansion = document.getElementById('task-expansion-' + taskId);
        var content = document.getElementById('task-panel-content');
        if (!card || !expansion || !content) return false;
        returnPanelContentToCard();
        content.appendChild(expansion);
        expansion.style.display = 'block';
        card.classList.add('expanded');
        var face = card.querySelector('.task-compact-face');
        if (face) face.setAttribute('aria-expanded', 'true');
        document.getElementById('task-panel-number').textContent = 'Task #' + taskId;
        document.getElementById('task-panel-title').textContent =
            (card.querySelector('.task-title-revamp') || {}).textContent || 'Task';
        switchExpansionTab(taskId, expandedCardTabs[taskId] || 'overview');
        if (reloadData) loadCardData(taskId);
        return true;
    }
    function closeTaskPanelNow() {
        if (!activeTaskId) return;
        var taskId = activeTaskId;
        returnPanelContentToCard();
        var card = document.getElementById('task-card-' + taskId);
        if (card) {
            card.classList.remove('expanded');
            var face = card.querySelector('.task-compact-face');
            if (face) face.setAttribute('aria-expanded', 'false');
        }
        var panel = document.getElementById('task-detail-panel');
        panel.hidden = true;
        panel.setAttribute('aria-hidden', 'true');
        document.getElementById('task-panel-backdrop').hidden = true;
        document.body.classList.remove('task-panel-open');
        activeTaskId = null;
        panelLoadSerial++;
        var focusTarget = panelReturnFocus && panelReturnFocus.isConnected
            ? panelReturnFocus : card;
        panelReturnFocus = null;
        if (focusTarget) focusTarget.focus({preventScroll: true});
    }
    function closeTaskPanel() {
        if (!activeTaskId) return;
        if (panelHistoryEntry && taskIdFromUrl() === activeTaskId) {
            history.back();
            return;
        }
        closeTaskPanelNow();
        var url = new URL(window.location.href);
        url.searchParams.delete('task');
        history.replaceState({}, '', url);
        panelHistoryEntry = false;
    }
    function openTaskPanel(taskId, options) {
        options = options || {};
        taskId = Number(taskId);
        if (!document.getElementById('task-card-' + taskId)) {
            showToast('Task #' + taskId + ' is not on this board', 'error');
            return;
        }
        if (activeTaskId && activeTaskId !== taskId) closeTaskPanelNow();
        if (!activeTaskId) {
            var trigger = document.activeElement;
            var sourceCard = document.getElementById('task-card-' + taskId);
            panelReturnFocus = sourceCard && sourceCard.contains(trigger) ? trigger : sourceCard;
        }
        activeTaskId = taskId;
        var panel = document.getElementById('task-detail-panel');
        panel.hidden = false;
        panel.setAttribute('aria-hidden', 'false');
        document.getElementById('task-panel-backdrop').hidden = false;
        document.body.classList.add('task-panel-open');
        expandedCardTabs[taskId] = options.tab || 'overview';
        attachTaskPanel(taskId, true);
        if (options.history !== false && taskIdFromUrl() !== taskId) {
            var url = new URL(window.location.href);
            url.searchParams.set('task', String(taskId));
            history.pushState({task: taskId}, '', url);
            panelHistoryEntry = true;
        }
        if (options.focus !== false) document.getElementById('task-panel-close').focus();
    }
    function initExpandedCards() {
        expandedCardTabs = getExpandedCardTabs();
        if (activeTaskId) {
            if (!attachTaskPanel(activeTaskId, true)) closeTaskPanelNow();
            return;
        }
        var linkedTask = taskIdFromUrl();
        if (linkedTask) openTaskPanel(linkedTask, {history: false});
    }
    function toggleCardExpansion(taskId) {
        if (activeTaskId === taskId) closeTaskPanel();
        else openTaskPanel(taskId);
    }
    function expandCardAndShowApprovals(taskId) {
        openTaskPanel(taskId, {tab: 'approvals'});
    }
    function switchExpansionTab(taskId, tab) {
        expandedCardTabs[taskId] = tab;
        var expansion = document.getElementById('task-expansion-' + taskId);
        if (!expansion) return;
        expansion.querySelectorAll('.expansion-tab').forEach(function(btn) {
            var selected = btn.dataset.tab === tab;
            btn.classList.toggle('active', selected);
            btn.setAttribute('aria-selected', selected ? 'true' : 'false');
        });
        expansion.querySelectorAll('.expansion-pane').forEach(function(pane) {
            var paneTab = pane.id.replace('expansion-pane-', '').replace('-' + taskId, '');
            var selected = paneTab === tab;
            pane.classList.toggle('active', selected);
            pane.setAttribute('aria-hidden', selected ? 'false' : 'true');
        });
        saveExpandedCardTabs();
    }
    function loadCardData(taskId) {
        fetchTaskOverview(taskId);
        fetchTaskApprovals(taskId);
        fetchTaskActivity(taskId);
        fetchTaskReviews(taskId);
        fetchTaskGitDiff(taskId);
        fetchTaskTerminal(taskId);
    }
    function priorityName(value) {
        var n = Number(value);
        if (n === 0) return 'None';
        if (n <= 3) return 'Low';
        if (n <= 6) return 'Normal';
        if (n <= 8) return 'High';
        return 'Urgent';
    }
    function updateReviewGateBadge(taskId, completion) {
        var card = document.getElementById('task-card-' + taskId);
        if (!card) return;
        var meta = card.querySelector('.task-meta-revamp');
        if (!meta) return;
        var badge = card.querySelector('.task-review-progress');
        if (!completion || (completion.stage !== 'review' && completion.stage !== 'done')) {
            if (badge) badge.remove();
            return;
        }
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'badge task-review-progress';
            meta.append(badge);
        }
        badge.textContent = 'Review ' + completion.completed + '/' + completion.total;
        badge.classList.toggle(
            'is-complete',
            completion.total > 0 && completion.completed === completion.total
        );
        badge.title = completion.blocker || 'All completion evidence is present';
    }

    function renderCompletionGates(completion) {
        if (!completion || (completion.stage !== 'review' && completion.stage !== 'done')) return '';
        var gates = (completion.gates || []).map(function(gate) {
            var stateLabel = gate.state === 'complete'
                ? 'Complete'
                : gate.state === 'not_required' ? 'Not required' : 'Waiting';
            return '<li class="completion-gate completion-gate-' + escapeHtml(gate.state) + '">' +
                '<span class="completion-gate-mark" aria-hidden="true">' +
                    (gate.state === 'complete' ? '✓' : gate.state === 'not_required' ? '–' : '○') +
                '</span><span><strong>' + escapeHtml(gate.label) + '</strong>' +
                '<small>' + escapeHtml(stateLabel + ' · ' + gate.detail) + '</small></span></li>';
        }).join('');
        var blocker = completion.blocker
            ? '<p class="completion-gate-blocker"><strong>Waiting:</strong> ' +
                escapeHtml(completion.blocker) + '</p>'
            : '<p class="completion-gate-ready">All required completion evidence is present.</p>';
        return '<section class="task-overview-section completion-gates-section">' +
            '<div class="completion-gates-heading"><h3>Completion gates</h3>' +
            '<span class="badge task-review-progress' +
                (completion.completed === completion.total ? ' is-complete' : '') + '">' +
                completion.completed + '/' + completion.total + '</span></div>' +
            '<ul class="completion-gates-list">' + gates + '</ul>' + blocker + '</section>';
    }

    function loadReviewGateBadges() {
        document.querySelectorAll('.kanban-task-revamp[data-status="in_review"], .kanban-task-revamp[data-status="completed"]').forEach(function(card) {
            var taskId = Number(card.dataset.taskId);
            apiFetch('/ui/tasks/' + taskId + '/completion-gates', {}, 'Could not load completion gates')
                .then(function(data) { updateReviewGateBadge(taskId, data); })
                .catch(function() {});
        });
    }

    function renderTaskOverview(taskId, task, sessions, approvals, completion) {
        if (activeTaskId !== taskId) return;
        var overview = document.getElementById('task-overview-' + taskId);
        var logs = document.getElementById('task-logs-' + taskId);
        if (!overview) return;
        var card = document.getElementById('task-card-' + taskId);
        if (card) card.dataset.version = task.version;
        document.getElementById('task-panel-title').textContent = task.title || 'Task';
        var columns = Array.from(document.querySelectorAll('.kanban-column-revamp'));
        var stage = columns.find(function(column) { return Number(column.dataset.stageId) === task.stage_id; });
        var stageName = stage ? stage.dataset.stageName : 'Unknown';
        var stageOptions = columns.map(function(column) {
            return '<option value="' + column.dataset.stageId + '"' +
                (Number(column.dataset.stageId) === task.stage_id ? ' selected' : '') + '>' +
                escapeHtml(column.dataset.stageName) + '</option>';
        }).join('');
        var owners = (task.assignees || []).map(function(agent) { return agent.name; });
        var latest = sessions && sessions.length ? sessions[0] : null;
        var execution = latest ? latest.status.charAt(0).toUpperCase() + latest.status.slice(1) : 'Not started';
        var blocker = approvals && approvals.length ? 'Approval requested' :
            completion && completion.stage === 'review' && completion.blocker ? completion.blocker :
            latest && latest.status === 'error' ? 'Last agent session failed' :
            latest && latest.status === 'blocked' ? 'Agent session blocked' :
            task.status === 'blocked' ? 'Task marked blocked' : 'None';
        updateReviewGateBadge(taskId, completion);
        var nextAction = approvals && approvals.length
            ? '<button class="btn btn-primary" type="button" onclick="openApprovalPopup(' + approvals[0].id + ')">Review approval</button>'
            : !(task.assignees || []).length
                ? '<button class="btn btn-primary" type="button" onclick="openAssignModal(' + taskId + ')">Assign work</button>'
                : stage && stage.dataset.stageKey === 'backlog'
                    ? (card.dataset.startState === 'eligible'
                        ? '<button class="btn btn-primary" type="button" onclick="startTask(' + taskId + ',this,\'eligible\')">Start</button>'
                        : '<button class="btn btn-primary" type="button" onclick="startTask(' + taskId + ',this,\'' + (card.dataset.startState || 'needs_assignment') + '\')">' +
                            (card.dataset.startState === 'choose_agent' ? 'Choose agent' : 'Assign agent') + '</button>')
                    : '<a class="btn btn-primary" href="/ui/projects/' + PROJECT_ID + '/workbench#terminal:task:' + taskId + '">View activity</a>';
        overview.innerHTML =
            '<section class="task-overview-section"><h3>Description</h3><p class="task-overview-description">' +
                escapeHtml(task.description || 'No description yet.') + '</p></section>' +
            renderCompletionGates(completion) +
            '<dl class="task-overview-facts">' +
                '<div><dt>Owner</dt><dd>' + escapeHtml(owners.join(', ') || 'Unassigned') + '</dd></div>' +
                '<div><dt>Priority</dt><dd>' + priorityName(task.priority) + ' (' + Number(task.priority) + ')</dd></div>' +
                '<div><dt>Board stage</dt><dd><select id="task-panel-stage" class="form-input" aria-label="Task stage">' +
                    stageOptions + '</select></dd></div>' +
                '<div><dt>Task status</dt><dd>' + escapeHtml(statusLabel(task.status)) + '</dd></div>' +
                '<div><dt>Execution</dt><dd>' + escapeHtml(execution) + '</dd></div>' +
                '<div><dt>Blocker</dt><dd>' + escapeHtml(blocker) + '</dd></div>' +
            '</dl><div class="task-overview-next"><span>Next action</span>' + nextAction + '</div>';
        var stageSelect = overview.querySelector('#task-panel-stage');
        if (stageSelect) stageSelect.addEventListener('change', function() {
            var target = document.querySelector('.kanban-drop-zone-revamp[data-stage-id="' + stageSelect.value + '"]');
            stageSelect.disabled = true;
            moveTaskCard(card, target).then(function(moved) {
                if (!moved) stageSelect.value = String(task.stage_id);
                else refreshBoardFromServer();
            }).finally(function() { stageSelect.disabled = false; });
        });
        if (logs) {
            var entries = (task.logs || []).slice().reverse();
            logs.innerHTML = entries.length ? entries.map(function(entry) {
                return '<div class="task-log-entry"><strong>' + escapeHtml(entry.log_type || 'Log') +
                    '</strong><p>' + escapeHtml(entry.message || '') + '</p></div>';
            }).join('') : '<p class="text-secondary">No task logs yet.</p>';
        }
        var editDialog = document.getElementById('task-modal');
        if (editDialog && editDialog.getAttribute('aria-hidden') === 'false' &&
            document.getElementById('task-form-id').value === String(taskId)) {
            var draftVersion = document.getElementById('task-form-version').value;
            if (draftVersion && Number(draftVersion) !== task.version) {
                setTaskFormError('This task changed while you were editing. Your draft is preserved; review the latest task before saving.');
            }
        }
    }
    async function fetchTaskOverview(taskId) {
        var serial = ++panelLoadSerial;
        var results = await Promise.allSettled([
            apiFetch('/tasks/' + taskId, {}, 'Could not load task'),
            apiFetch('/agents/sessions?task_id=' + taskId + '&limit=1', {}, 'Could not load sessions'),
            apiFetch('/agents/approvals?task_id=' + taskId + '&status_filter=pending&limit=10', {}, 'Could not load approvals'),
            apiFetch('/ui/tasks/' + taskId + '/completion-gates', {}, 'Could not load completion gates')
        ]);
        if (serial !== panelLoadSerial || activeTaskId !== taskId) return;
        if (results[0].status !== 'fulfilled') {
            var overview = document.getElementById('task-overview-' + taskId);
            if (overview) overview.textContent = results[0].reason.message;
            return;
        }
        renderTaskOverview(taskId, results[0].value,
            results[1].status === 'fulfilled' ? results[1].value : [],
            results[2].status === 'fulfilled' ? results[2].value : [],
            results[3].status === 'fulfilled' ? results[3].value : null);
    }
    function editSelectedTask() {
        var card = activeTaskId && document.getElementById('task-card-' + activeTaskId);
        if (card) openEditModal(card);
    }
    window.addEventListener('popstate', function() {
        var linkedTask = taskIdFromUrl();
        panelHistoryEntry = false;
        if (linkedTask) openTaskPanel(linkedTask, {history: false});
        else closeTaskPanelNow();
    });
    document.addEventListener('keydown', function(event) {
        var panel = document.getElementById('task-detail-panel');
        if (!activeTaskId || !panel || panel.hidden ||
            document.querySelector('.modal-overlay[aria-hidden="false"]')) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            closeTaskPanel();
        } else if (event.key === 'Tab') {
            var focusable = Array.from(panel.querySelectorAll('button:not([disabled]), a[href], select:not([disabled]), input:not([disabled])'))
                .filter(function(node) { return node.getClientRects().length > 0; });
            if (!focusable.length) return;
            if (event.shiftKey && document.activeElement === focusable[0]) {
                event.preventDefault();
                focusable[focusable.length - 1].focus();
            } else if (!event.shiftKey && document.activeElement === focusable[focusable.length - 1]) {
                event.preventDefault();
                focusable[0].focus();
            }
        }
    }, true);
