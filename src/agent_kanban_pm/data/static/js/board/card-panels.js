    // --- In-card terminal ---
    async function fetchTaskTerminal(taskId) {
        var pre = document.getElementById('task-terminal-pre-' + taskId);
        if (!pre) return;
        try {
            var resp = await fetch('/agents/tasks/' + taskId + '/active-session', {
                headers: {'X-Entity-ID': CURRENT_ENTITY_ID || ''}
            });
            if (!resp.ok) throw new Error('Could not load task session');
            var session = await resp.json();
            if (!session || !session.id) {
                var recentResp = await fetch('/agents/sessions?task_id=' + taskId + '&limit=1');
                if (!recentResp.ok) throw new Error('Could not load recent task session');
                var recentSessions = await recentResp.json();
                session = recentSessions[0];
            }
            if (!session || !session.id) { pre.textContent = 'No session output for this task.'; return; }
            pre.dataset.sessionId = session.id;
            var termResp = await fetch('/agents/sessions/' + session.id + '/terminal?limit=50');
            if (!termResp.ok) { pre.textContent = 'Failed to load terminal.'; return; }
            var data = await termResp.json();
            var atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
            var scrollTop = pre.scrollTop;
            pre.textContent = window.KanbanTerminalFeed.focused(data.activities, 12);
            pre.scrollTop = atBottom ? pre.scrollHeight : scrollTop;
        } catch(e) { pre.textContent = 'Error: ' + e.message; }
    }
    function popOutTerminal(taskId) {
        window.location.href = '/ui/projects/' + PROJECT_ID + '/workbench#terminal:task:' + taskId;
    }

    // --- Task approvals in card ---
    async function fetchTaskApprovals(taskId) {
        try {
            var resp = await fetch('/agents/approvals?task_id=' + taskId + '&status_filter=pending&limit=20', {
                headers: {'X-Entity-ID': CURRENT_ENTITY_ID || ''}
            });
            if (!resp.ok) return;
            var approvals = await resp.json();
            var pending = approvals;
            var el = document.getElementById('task-approvals-list-' + taskId);
            if (!el) return;
            if (!pending.length) {
                el.innerHTML = '<p class="text-secondary" style="font-size:0.8rem;">No pending approvals for this task.</p>';
            } else {
                el.innerHTML = pending.slice(0, 10).map(function(a) {
                    return renderApprovalItem(a, true);
                }).join('');
            }
            var badge = document.getElementById('task-approval-badge-' + taskId);
            if (badge) {
                if (pending.length > 0) {
                    badge.style.display = '';
                    badge.textContent = '\u26A0 ' + pending.length;
                } else {
                    badge.style.display = 'none';
                }
            }
            var countBadge = document.getElementById('task-approvals-count-' + taskId);
            if (countBadge) {
                if (pending.length > 0) {
                    countBadge.style.display = '';
                    countBadge.textContent = pending.length;
                } else {
                    countBadge.style.display = 'none';
                }
            }
            updateCardApprovalIndicator(taskId, pending.length);
        } catch(e) { console.error('fetchTaskApprovals', e); }
    }
    function updateCardApprovalIndicator(taskId, pendingCount) {
        var card = document.getElementById('task-card-' + taskId);
        if (!card) return;
        var indicators = card.querySelector('.task-state-indicators');
        if (!indicators) return;
        var existingBadge = indicators.querySelector('.task-indicator.pending-approval');
        if (pendingCount > 0 && !existingBadge) {
            var dot = document.createElement('span');
            dot.className = 'task-indicator pending-approval';
            dot.title = pendingCount + ' pending approval(s)';
            indicators.appendChild(dot);
            card.classList.add('has-pending-approval');
        } else if (pendingCount === 0 && existingBadge) {
            existingBadge.remove();
            card.classList.remove('has-pending-approval');
        }
    }
    function renderApprovalItem(a, withControls) {
        var statusColors = { pending:'#fbbf24', approved:'#34d399', rejected:'#f87171', cancelled:'#9ca3af', expired:'#9ca3af' };
        var color = statusColors[a.status] || '#9ca3af';
        var time = a.requested_at ? timeAgo(a.requested_at) : '';
        var controls = withControls
            ? '<div class="approval-controls"><button class="btn btn-primary btn-sm" onclick="event.stopPropagation();openApprovalPopup(' + a.id + ')">Review request</button></div>'
            : (a.response_message ? '<div class="activity-detail">"' + escapeHtml(a.response_message) + '"</div>' : '');
        return '<div class="insight-item">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;gap:0.5rem;">' +
            '<span class="insight-title">' + escapeHtml(a.title) + '</span>' +
            '<span style="background:' + color + ';color:#111;padding:0.1rem 0.5rem;border-radius:1rem;font-size:0.7rem;font-weight:700;">' + a.status + '</span></div>' +
            '<div class="activity-detail">' + escapeHtml(a.approval_type) + ' · agent #' + a.agent_id + (a.task_id ? ' · task #' + a.task_id : '') + (a.session_id ? ' · session #' + a.session_id : '') + '</div>' +
            '<div style="margin-top:0.35rem;font-size:0.8rem;">' + escapeHtml(a.message) + '</div>' +
            '<div class="insight-meta">' + time + '</div>' +
            controls + '</div>';
    }

    // --- Task activity in card ---
    function renderActivityEntries(entries, compact) {
        if (!entries.length) return '<p class="text-secondary" style="font-size:0.8rem;">No activity recorded.</p>';
        return entries.map(function(entry) {
            var stamp = entry.created_at ? new Date(entry.created_at).toLocaleTimeString() : '';
            var type = entry.activity_type || entry.source || 'update';
            var typeClass = type === 'tool_use' ? 'act-cmd' : type === 'file_edit' || type === 'file_read' ? 'act-file' : type === 'error' ? 'act-error' : 'act-msg';
            var body = '';
            if (entry.command) body += '<div class="act-cmd">$ ' + escapeHtml(entry.command) + '</div>';
            if (entry.file_path) body += '<div class="act-file">📄 ' + escapeHtml(entry.file_path) + '</div>';
            if (entry.message) body += '<div class="' + typeClass + '">' + escapeHtml(entry.message) + '</div>';
            return '<div class="act-entry"><div class="act-meta"><span class="act-type">' + escapeHtml(type) + '</span><span>' + stamp + '</span></div>' + body + '</div>';
        }).join('');
    }
    async function fetchTaskActivity(taskId) {
        try {
            var resp = await fetch('/agents/activity?task_id=' + taskId + '&limit=20');
            var el = document.getElementById('task-activity-list-' + taskId);
            if (!el) return;
            if (!resp.ok) { el.innerHTML = '<p class="text-secondary" style="font-size:0.8rem;">No activity recorded.</p>'; return; }
            var entries = await resp.json();
            el.innerHTML = renderActivityEntries(entries.reverse(), true);
        } catch(e) { console.error('fetchTaskActivity', e); }
    }

    // --- Activity popup ---
    async function openActivityPopup(taskId) {
        var overlay = document.getElementById('activity-popup-overlay');
        var title = document.getElementById('activity-popup-title');
        var body = document.getElementById('activity-popup-body');
        if (!overlay) return;
        title.textContent = 'Activity — Task #' + taskId;
        body.innerHTML = '<p class="text-secondary">Loading…</p>';
        openModal('activity-popup-overlay');
        try {
            var entries = await apiFetch(
                '/agents/activity?task_id=' + taskId + '&limit=200',
                {},
                'Failed to load activity'
            );
            body.innerHTML = renderActivityEntries(entries.reverse(), false);
        } catch(e) { body.innerHTML = '<p class="text-secondary">' + escapeHtml(e.message) + '</p>'; }
    }
    function closeActivityPopup() {
        closeModal('activity-popup-overlay');
    }

    // --- Approval popup ---
    function approvalById(approvalId) {
        return allPendingApprovals.find(function(a) { return String(a.id || a.approval_id) === String(approvalId); }) || null;
    }
    function cacheApprovalFromEvent(data) {
        if (!data) return null;
        var approval = {
            id: data.id || data.approval_id,
            project_id: data.project_id,
            task_id: data.task_id,
            session_id: data.session_id,
            agent_id: data.agent_id,
            approval_type: data.approval_type || 'other',
            title: data.title || data.approval_type || 'Approval needed',
            message: data.message || '',
            command: data.command || '',
            diff_content: data.diff_content || '',
            status: data.status || 'pending',
            requested_at: data.requested_at || new Date().toISOString()
        };
        if (!approval.id) return null;
        var existing = approvalById(approval.id);
        if (existing) Object.assign(existing, approval);
        else allPendingApprovals.unshift(approval);
        return approval;
    }
    function renderApprovalPopup(a) {
        var title = document.getElementById('approval-popup-title');
        var body = document.getElementById('approval-popup-body');
        var note = document.getElementById('approval-popup-note');
        if (!title || !body) return;
        title.textContent = a.title || a.approval_type || 'Approval needed';
        if (note) note.value = '';
        var fields = [
            ['Type', a.approval_type || 'other'],
            ['Agent', a.agent_id ? '#' + a.agent_id : 'unknown'],
            ['Task', a.task_id ? '#' + a.task_id : 'project level'],
            ['Session', a.session_id ? '#' + a.session_id : 'none']
        ].map(function(pair) {
            return '<div class="approval-popup-field"><span>' + escapeHtml(pair[0]) + '</span><strong>' + escapeHtml(pair[1]) + '</strong></div>';
        }).join('');
        var command = a.command ? '<details open><summary>Requested action</summary><pre class="approval-popup-command">' + escapeHtml(a.command) + '</pre></details>' : '';
        var diff = a.diff_content ? '<details><summary>Proposed diff (' + String(a.diff_content).split('\n').length + ' lines)</summary><pre class="approval-popup-diff">' + escapeHtml(String(a.diff_content).substring(0, 8000)) + '</pre></details>' : '';
        body.innerHTML =
            '<div class="approval-popup-message">' + escapeHtml(a.message || 'The agent is waiting for approval to continue.') + '</div>' +
            '<div class="approval-popup-grid">' + fields + '</div>' +
            command + diff;
    }
    function openApprovalPopup(approvalId) {
        var approval = approvalById(approvalId);
        if (!approval) {
            fetchAgentApprovals().then(function() {
                var refreshed = approvalById(approvalId);
                if (refreshed) openApprovalPopup(approvalId);
            });
            return;
        }
        currentApprovalId = approval.id || approval.approval_id;
        renderApprovalPopup(approval);
        openModal('approval-popup-overlay');
        var dd = document.getElementById('bell-dropdown');
        if (dd) dd.style.display = 'none';
    }
    function openTaskApprovalPopup(taskId) {
        var approval = allPendingApprovals.find(function(a) { return String(a.task_id) === String(taskId); });
        if (approval) openApprovalPopup(approval.id);
    }
    function openApprovalPopupFromEvent(data) {
        var approval = cacheApprovalFromEvent(data);
        if (approval) openApprovalPopup(approval.id);
    }
    function closeApprovalPopup() {
        closeModal('approval-popup-overlay');
    }
    function resolveCurrentApproval(decision) {
        if (!currentApprovalId) return;
        resolveApproval(currentApprovalId, decision);
    }
