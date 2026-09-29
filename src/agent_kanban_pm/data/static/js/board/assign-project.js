    // --- Assignment ---
    window.openAssignModal = function(taskId) {
        document.getElementById('assign-task-id').value = taskId;
        document.getElementById('assign-entity-list').innerHTML = '<p class="text-secondary">Loading roles...</p>';
        openModal('assign-modal');
        apiFetch('/ui/api/roles', {}, 'Failed to load roles').then(function(data) {
            var html = '';
            (data.roles || []).forEach(function(r) {
                var disabled = r.installed ? '' : 'disabled';
                var model = r.model ? ' \u00B7 ' + r.model : '';
                var unavailMsg = !r.installed ? '<small class="text-danger" style="display:block;font-size:0.75rem;margin-top:0.2rem;">CLI \'' + r.command + '\' not found on PATH</small>' : '';
                var unavailTitle = !r.installed ? ' title="Cannot assign: CLI \'' + r.command + '\' is not installed on PATH"' : '';
                html += '<div class="entity-item"><span><strong>' + r.role + '</strong> \u2192 ' + r.display_name + '<small class="text-secondary"> (' + r.command + model + ')</small>' + unavailMsg + '</span><button class="btn btn-sm btn-primary" ' + disabled + unavailTitle + ' onclick="assignRoleToTask(' + taskId + ',\'' + r.role + '\',this)">Assign</button></div>';
            });
            if (!html) {
                html = '<div class="empty-state text-center" style="padding:1.5rem 0;"><p class="text-secondary" style="margin-bottom:0.75rem;">No roles configured.</p><button class="btn btn-sm btn-primary" type="button" onclick="closeModal(\'assign-modal\');openTeamModal();">Configure agents</button></div>';
            }
            document.getElementById('assign-entity-list').innerHTML = html;
        }).catch(function() { document.getElementById('assign-entity-list').innerHTML = '<p class="text-danger">Failed to load roles.</p>'; });
    };
    window.toggleSetupChecklist = function() {
        var grid = document.getElementById('setup-steps-grid');
        var icon = document.getElementById('checklist-toggle-icon');
        if (!grid) return;
        if (grid.style.display === 'none') {
            grid.style.display = 'grid';
            if (icon) icon.innerHTML = '&#9650;';
        } else {
            grid.style.display = 'none';
            if (icon) icon.innerHTML = '&#9660;';
        }
    };
    window.openWorkspaceFolderPicker = function() {
        editProject();
        openFolderPicker('project-form-path');
    };
    window.assignRoleToTask = function(taskId, roleName, btn, overrideReason) {
        btn.disabled = true; btn.textContent = '...';
        apiFetch('/ui/tasks/' + taskId + '/assign-role', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID },
            body: JSON.stringify({ role: roleName, override_reason: overrideReason || '' })
        }, 'Failed to assign role')
            .then(function() {
                showToast('Assigned to ' + roleName, 'success');
                closeModal('assign-modal');
                return refreshBoardFromServer();
            })
            .catch(function(err) {
                if (!overrideReason && err.message.toLowerCase().indexOf('override reason is required') !== -1) {
                    btn.disabled = false; btn.textContent = 'Assign';
                    return requestPolicyOverride({
                        parentModalId: 'assign-modal',
                        title: 'Start work anyway?',
                        actionLabel: 'Assign and start anyway',
                        blocker: err.message.replace(/\.? A human override reason is required to start work$/i, ''),
                        evidenceLabel: 'Starting this worker is blocked by:',
                        gates: []
                    }).then(function(decision) {
                        if (decision && decision.proceed) {
                            return window.assignRoleToTask(taskId, roleName, btn, decision.reason);
                        }
                    });
                }
                showToast('Error: ' + err.message, 'error');
                btn.disabled = false; btn.textContent = 'Assign';
            });
    };
    window.assignEntity = function(taskId, entityId, btn) {
        btn.disabled = true; btn.textContent = '...';
        apiFetch('/ui/tasks/' + taskId + '/assign', { method: 'POST', headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID }, body: JSON.stringify({ entity_id: entityId, action: 'assign' }) }, 'Failed to assign task')
            .then(function() { showToast('Assigned!', 'success'); closeModal('assign-modal'); return refreshBoardFromServer(); })
            .catch(function(err) { showToast('Error: ' + err.message, 'error'); btn.disabled = false; btn.textContent = 'Assign'; });
    };
    window.unassignEntity = function(taskId, entityId, badge) {
        apiFetch('/ui/tasks/' + taskId + '/assign', { method: 'POST', headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID }, body: JSON.stringify({ entity_id: entityId, action: 'unassign' }) }, 'Failed to unassign task')
            .then(function() { badge.remove(); showToast('Unassigned', 'success'); })
            .catch(function(err) { showToast('Error: ' + err.message, 'error'); });
    };

    // --- Project ---
    window.editProject = function() { openModal('project-modal'); };
    window.submitProjectEdit = function() {
        var name = document.getElementById('project-form-name').value.trim();
        var desc = document.getElementById('project-form-desc').value.trim();
        var path = document.getElementById('project-form-path').value.trim();
        if (!name) return;
        apiFetch('/ui/projects/' + PROJECT_ID + '/edit', { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID }, body: JSON.stringify({ name: name, description: desc, path: path }) }, 'Failed to update project')
            .then(function() { location.reload(); })
            .catch(function(err) { showToast('Error: ' + err.message, 'error'); });
    };
    window.openStagePolicyModal = function() {
        openModal('stage-policy-modal');
        loadStagePolicies();
    };
    function loadStagePolicies() {
        var list = document.getElementById('stage-policy-list');
        list.innerHTML = '<p class="text-secondary">Loading policies...</p>';
        apiFetch('/agents/projects/' + PROJECT_ID + '/stage-policies', {headers: {'x-entity-id': CURRENT_ENTITY_ID}}, 'Failed to load stage policies')
            .then(function(policies) {
                if (!policies.length) { list.innerHTML = '<p class="text-secondary">No policies yet. Click "Seed Defaults" to create standard policies for this project.</p>'; return; }
                var roleHints = ['orchestrator', 'ui', 'architecture', 'worker', 'test', 'diff_review', 'git_pr'];
                var reviewModes = ['none', 'auto', 'human', 'auto_then_human_for_critical'];
                var html = policies.map(function(p) {
                    var roles = (p.on_enter_roles || []).join(', ') || 'none';
                    var outputs = (p.required_outputs || []).join(', ') || 'none';
                    var reviewMode = p.review_mode || 'none';
                    var parallel = p.allow_parallel ? 'Yes' : 'No';
                    var needsMove = p.requires_orchestrator_move ? 'Yes' : 'No';
                    return '<div class="entity-item" style="padding:0.75rem;border-bottom:1px solid var(--border-color);">' +
                        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;">' +
                        '<div><strong>' + p.stage_key + '</strong></div>' +
                        '<div><small class="text-secondary">Review: ' + reviewMode + '</small></div>' +
                        '<div><small>Roles: ' + roles + '</small></div>' +
                        '<div><small>Outputs: ' + outputs + '</small></div>' +
                        '<div><small>Parallel: ' + parallel + '</small></div>' +
                        '<div><small>Orchestrator move: ' + needsMove + '</small></div>' +
                        '</div></div>';
                }).join('');
                list.innerHTML = html;
            })
            .catch(function(err) { list.innerHTML = '<p class="text-danger">' + escapeHtml(err.message) + '</p>'; });
    }
    window.seedDefaultPolicies = function() {
        apiFetch('/agents/projects/' + PROJECT_ID + '/stage-policies/defaults', {method: 'POST', headers: {'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID}}, 'Failed to seed policies')
            .then(function() { showToast('Default stage policies seeded', 'success'); loadStagePolicies(); })
            .catch(function(err) { showToast('Failed to seed policies: ' + err.message, 'error'); });
    };
    window.openTeamModal = function() {
        openModal('team-modal');
        document.getElementById('team-list').innerHTML = '<p class="text-secondary">Loading roles...</p>';
        apiFetch('/ui/api/roles', {}, 'Failed to load roles').then(function(data) { renderRoleCliPanel(data); }).catch(function(err) { document.getElementById('team-list').innerHTML = '<p class="text-danger">' + escapeHtml(err.message) + '</p>'; });
    };
    // --- Approval Queue ---
    var allPendingApprovals = [];
    var currentApprovalId = null;
    var boardLoadedAt = Date.now();
    async function fetchAgentApprovals() {
        try {
            var response = await fetch('/agents/approvals?project_id=' + PROJECT_ID + '&status_filter=pending&limit=50', { headers: {'X-Entity-ID': CURRENT_ENTITY_ID || ''} });
            var pending = response.ok ? await response.json() : [];
            updateApprovalsBadge(pending.length);
            allPendingApprovals = pending;
            updateHeaderBell(pending);

            // Update per-task approval indicators
            document.querySelectorAll('.kanban-task-revamp').forEach(function(card) {
                updateCardApprovalIndicator(parseInt(card.dataset.taskId), 0);
            });
            var taskApprovalCounts = {};
            pending.forEach(function(a) { if (a.task_id) taskApprovalCounts[a.task_id] = (taskApprovalCounts[a.task_id] || 0) + 1; });
            Object.keys(taskApprovalCounts).forEach(function(tid) { updateCardApprovalIndicator(parseInt(tid), taskApprovalCounts[tid]); });
        } catch(e) { console.error('fetchAgentApprovals', e); }
    }
    function updateApprovalsBadge(count) {
        var badge = document.getElementById('approvals-badge');
        if (!badge) return;
        if (count > 0) { badge.textContent = count; badge.style.display = ''; } else { badge.style.display = 'none'; }
    }
    function updateHeaderBell(pending) {
        var countEl = document.getElementById('header-bell-count');
        var listEl = document.getElementById('bell-dropdown-list');
        if (countEl) {
            if (pending.length > 0) { countEl.textContent = pending.length; countEl.style.display = ''; }
            else { countEl.style.display = 'none'; }
        }
        if (listEl) {
            if (!pending.length) { listEl.innerHTML = '<p class="text-secondary" style="font-size:0.8rem;">No pending approvals.</p>'; }
            else {
                listEl.innerHTML = pending.slice(0, 10).map(function(a) {
                    return '<div class="insight-item" style="cursor:pointer;border-left:3px solid #fbbf24;" onclick="openApprovalPopup(' + a.id + ')"><div style="display:flex;justify-content:space-between;align-items:center;"><span class="insight-title">' + escapeHtml(a.title || a.approval_type) + '</span><span style="background:#fbbf24;color:#111;padding:0.1rem 0.5rem;border-radius:1rem;font-size:0.7rem;font-weight:700;">pending</span></div><div class="insight-meta">' + escapeHtml(a.approval_type) + ' \u00B7 ' + (a.task_id ? 'Task #' + a.task_id + ' \u00B7 ' : '') + timeAgo(a.requested_at) + '</div></div>';
                }).join('');
            }
        }
    }
    function renderApprovalList(elementId, approvals, withControls, emptyText) {
        var el = document.getElementById(elementId);
        if (!el) return;
        if (!approvals.length) { el.innerHTML = '<p class="text-secondary" style="font-size:0.8rem;">' + emptyText + '</p>'; return; }
        el.innerHTML = approvals.map(function(a) { return renderApprovalItem(a, withControls); }).join('');
    }
    async function resolveApproval(approvalId, decision) {
        var note = (document.getElementById('approval-popup-note') || {}).value || (document.getElementById('approval-note-' + approvalId) || {}).value || '';
        try {
            await apiFetch('/agents/approvals/' + approvalId + '/resolve', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', 'X-Entity-ID': CURRENT_ENTITY_ID || '' },
                body: JSON.stringify({ decision: decision, response_message: note || null })
            }, 'Failed to resolve approval');
            showToast('Approval ' + decision, 'success');
            closeApprovalPopup();
            currentApprovalId = null;
            fetchAgentApprovals();
            // Refresh in-card approvals for any expanded cards
            document.querySelectorAll('[id^="task-approvals-list-"]').forEach(function(el) {
                var taskId = el.id.replace('task-approvals-list-', '');
                fetchTaskApprovals(parseInt(taskId));
            });
        } catch(e) { showToast('Error: ' + e.message, 'error'); }
    }

    // --- Open workspace ---
    async function openWorkspace(path) {
        if (!path) { showToast('No workspace folder set for this project', 'warning'); return; }
        try {
            var res = await fetch('/ui/api/open-workspace', { method: 'POST', headers: {'Content-Type': 'application/json', 'X-Entity-ID': CURRENT_ENTITY_ID || ''}, body: JSON.stringify({path: path}) });
            var data = await res.json().catch(function() { return {}; });
            if (res.ok) showToast('Opened ' + (data.path || path), 'success');
            else showToast('Could not open: ' + (data.detail || res.statusText), 'error');
        } catch(e) { showToast('Open failed: ' + e.message, 'error'); }
    }
    async function syncGithub() {
        showToast('Syncing GitHub contributions...', 'info');
        try {
            var resp = await fetch('/agents/projects/' + PROJECT_ID + '/contributions/sync/github', { method: 'POST', headers: {'Content-Type': 'application/json', 'X-Entity-ID': CURRENT_ENTITY_ID || ''} });
            var data = await resp.json();
            if (resp.ok) { showToast('Synced ' + (data.created || 0) + ' new items (' + (data.seen || 0) + ' total)', 'success'); }
            else showToast('Sync failed: ' + (data.detail || 'Unknown error'), 'error');
        } catch(err) { showToast('Sync error: ' + err.message, 'error'); }
    }
