    var folderPickerTarget = null;
    var folderPickerCurrent = '';
    var folderPickerParent = null;
    var folderPickerHome = '';
    var expandedCards = {};
    var expandedCardTabs = {};
    var draggedTask = null;


    // --- Notification settings ---
    function initNotificationSettings() {
        var enabled = localStorage.getItem('kanban.notifications.enabled') === 'true';
        var sound = localStorage.getItem('kanban.notifications.sound') === 'true';
        var cb = document.getElementById('notifications-enabled');
        var scb = document.getElementById('notifications-sound');
        if (cb) cb.checked = enabled;
        if (scb) scb.checked = sound;
        if (enabled && 'Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
    }
    function toggleNotifications(enabled) {
        localStorage.setItem('kanban.notifications.enabled', enabled);
        if (enabled && 'Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
    }
    function toggleNotificationSound(enabled) {
        localStorage.setItem('kanban.notifications.sound', enabled);
    }
    function sendOSNotification(title, body, taskId) {
        if (localStorage.getItem('kanban.notifications.enabled') !== 'true') return;
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        var n = new Notification(title, { body: body, tag: 'kanban-approval-' + taskId });
        if (taskId) {
            n.onclick = function() {
                window.focus();
                expandCardAndShowApprovals(taskId);
                n.close();
            };
        }
    }
    function toggleNotificationSettings() {
        var panel = document.getElementById('notification-settings-panel');
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    }

    // --- Bell dropdown ---
    function toggleBellDropdown() {
        var dd = document.getElementById('bell-dropdown');
        dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
    }
    document.addEventListener('click', function(e) {
        var bell = document.getElementById('header-bell-btn');
        var dd = document.getElementById('bell-dropdown');
        if (bell && dd && !bell.contains(e.target) && !dd.contains(e.target)) {
            dd.style.display = 'none';
        }
        var nsp = document.getElementById('notification-settings-panel');
        var nsb = document.querySelector('.notification-settings-btn');
        if (nsb && nsp && !nsb.contains(e.target) && !nsp.contains(e.target)) {
            nsp.style.display = 'none';
        }
    });

    // --- Card expansion ---
    function getExpandedCards() {
        try {
            var stored = localStorage.getItem('kanban.expanded.' + PROJECT_ID);
            return stored ? JSON.parse(stored) : {};
        } catch(e) { return {}; }
    }
    function saveExpandedCards() {
        try { localStorage.setItem('kanban.expanded.' + PROJECT_ID, JSON.stringify(expandedCards)); } catch(e) {}
    }
    function getExpandedCardTabs() {
        try {
            var stored = localStorage.getItem('kanban.expandedTabs.' + PROJECT_ID);
            return stored ? JSON.parse(stored) : {};
        } catch(e) { return {}; }
    }
    function saveExpandedCardTabs() {
        try { localStorage.setItem('kanban.expandedTabs.' + PROJECT_ID, JSON.stringify(expandedCardTabs)); } catch(e) {}
    }
    function initExpandedCards() {
        expandedCards = getExpandedCards();
        expandedCardTabs = getExpandedCardTabs();
        Object.keys(expandedCards).forEach(function(taskId) {
            if (expandedCards[taskId]) {
                var panel = document.getElementById('task-expansion-' + taskId);
                if (panel) {
                    panel.style.display = '';
                    var card = document.getElementById('task-card-' + taskId);
                    if (card) {
                        card.classList.add('expanded');
                        var face = card.querySelector('.task-compact-face');
                        if (face) face.setAttribute('aria-expanded', 'true');
                        switchExpansionTab(taskId, expandedCardTabs[taskId] || 'approvals');
                        loadCardData(taskId);
                    }
                }
            }
        });
    }
    function toggleCardExpansion(taskId) {
        var panel = document.getElementById('task-expansion-' + taskId);
        var card = document.getElementById('task-card-' + taskId);
        if (!panel || !card) return;
        if (panel.style.display === 'none') {
            panel.style.display = '';
            card.classList.add('expanded');
            var face = card.querySelector('.task-compact-face');
            if (face) face.setAttribute('aria-expanded', 'true');
            expandedCards[taskId] = true;
            var tab = expandedCardTabs[taskId] || 'approvals';
            switchExpansionTab(taskId, tab);
            loadCardData(taskId);
        } else {
            panel.style.display = 'none';
            card.classList.remove('expanded');
            var collapsedFace = card.querySelector('.task-compact-face');
            if (collapsedFace) collapsedFace.setAttribute('aria-expanded', 'false');
            expandedCards[taskId] = false;
        }
        saveExpandedCards();
    }
    function expandCardAndShowApprovals(taskId) {
        var panel = document.getElementById('task-expansion-' + taskId);
        var card = document.getElementById('task-card-' + taskId);
        if (panel && card) {
            panel.style.display = '';
            card.classList.add('expanded');
            expandedCards[taskId] = true;
        }
        switchExpansionTab(taskId, 'approvals');
        saveExpandedCards();
        loadCardData(taskId);
    }
    function switchExpansionTab(taskId, tab) {
        expandedCardTabs[taskId] = tab;
        var card = document.getElementById('task-card-' + taskId);
        if (!card) return;
        card.querySelectorAll('.expansion-tab').forEach(function(btn) {
            btn.classList.toggle('active', btn.dataset.tab === tab);
        });
        card.querySelectorAll('.expansion-pane').forEach(function(pane) {
            var paneTab = pane.id.replace('expansion-pane-', '').replace('-' + taskId, '');
            pane.classList.toggle('active', paneTab === tab);
        });
        saveExpandedCardTabs();
    }
    function loadCardData(taskId) {
        fetchTaskApprovals(taskId);
        fetchTaskActivity(taskId);
        fetchTaskReviews(taskId);
        fetchTaskTerminal(taskId);
    }

    // --- In-card terminal ---
    async function fetchTaskTerminal(taskId) {
        var pre = document.getElementById('task-terminal-pre-' + taskId);
        if (!pre) return;
        try {
            var resp = await fetch('/agents/tasks/' + taskId + '/active-session', {
                headers: {'X-Entity-ID': CURRENT_ENTITY_ID || ''}
            });
            if (!resp.ok) { pre.textContent = 'No active session for this task.'; return; }
            var session = await resp.json();
            if (!session || !session.id) { pre.textContent = 'No active session for this task.'; return; }
            pre.dataset.sessionId = session.id;
            var termResp = await fetch('/agents/sessions/' + session.id + '/terminal?limit=50');
            if (!termResp.ok) { pre.textContent = 'Failed to load terminal.'; return; }
            var data = await termResp.json();
            var lines = [];
            lines.push('$ ' + escapeHtml(data.session.command || 'session started'));
            lines.push('# workspace: ' + escapeHtml(data.session.workspace_path || ''));
            lines.push('');
            (data.activities || []).forEach(function(e) {
                var stamp = e.created_at ? new Date(e.created_at).toLocaleTimeString() : '';
                var type = e.activity_type || e.source || 'event';
                lines.push('[' + stamp + '] ' + type + ' ' + escapeHtml(e.message || ''));
                if (e.command) lines.push('  $ ' + escapeHtml(e.command));
                if (e.file_path) lines.push('  file: ' + escapeHtml(e.file_path));
            });
            pre.textContent = lines.join('\n').trim() || 'No activity recorded.';
            pre.scrollTop = pre.scrollHeight;
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

    // --- Task reviews in card ---
    async function fetchTaskReviews(taskId) {
        try {
            var resp = await fetch('/agents/projects/' + PROJECT_ID + '/diff-reviews?limit=20');
            if (!resp.ok) return;
            var reviews = await resp.json();
            var el = document.getElementById('task-reviews-list-' + taskId);
            if (!el) return;
            var taskReviews = reviews.filter(function(r) { return r.task_id === taskId; });
            if (!taskReviews.length) {
                el.innerHTML = '<p class="text-secondary" style="font-size:0.8rem;">No diff reviews for this task.</p>';
                return;
            }
            var statusColors = {pending:'#fbbf24', approved:'#34d399', rejected:'#f87171', changes_requested:'#60a5fa'};
            el.innerHTML = taskReviews.map(function(r) {
                var color = statusColors[r.status] || '#9ca3af';
                return '<div class="insight-item"><div style="display:flex;justify-content:space-between;align-items:center;"><span class="insight-title">Review #' + r.id + '</span><span style="background:' + color + ';color:#111;padding:0.1rem 0.5rem;border-radius:1rem;font-size:0.7rem;font-weight:700;">' + r.status + '</span></div>' +
                    (r.summary ? '<p style="font-size:0.78rem;margin:0.3rem 0;">' + escapeHtml(r.summary.substring(0,120)) + '</p>' : '') +
                    (r.is_critical ? '<span style="color:#ef4444;font-size:0.75rem;">Critical path</span>' : '') +
                    '<div class="insight-meta">' + timeAgo(r.created_at) + '</div></div>';
            }).join('');
        } catch(e) { console.error('fetchTaskReviews', e); }
    }

    function updateCardSessionIndicator(taskId, session) {
        var card = document.getElementById('task-card-' + taskId);
        if (!card) return;
        var indicators = card.querySelector('.task-state-indicators');
        if (!indicators) return;
        var existingChip = card.querySelector('.task-session-chip');
        var existingDot = indicators.querySelector('.task-indicator.active-session');
        if (session) {
            if (!existingDot) {
                var dot = document.createElement('span');
                dot.className = 'task-indicator active-session';
                dot.title = 'Active session';
                indicators.appendChild(dot);
            }
            card.classList.add('has-active-session');
            if (existingChip) {
                existingChip.style.display = '';
                existingChip.textContent = '\u25B6 live';
            }
        } else {
            if (existingDot) existingDot.remove();
            card.classList.remove('has-active-session');
            if (existingChip) existingChip.style.display = 'none';
        }
    }
    // --- Utilities ---
    var toastTimer = null;
    var toastHoldUntil = 0;
    // Toasts share one element. A toast raised by the user's own action is the
    // only confirmation that their click did anything, so it holds the slot
    // briefly against the WebSocket echo of that same change, which otherwise
    // overwrites it within milliseconds. Pass background:true for broadcasts.
    function showToast(message, type, options) {
        var opts = options || {};
        var now = Date.now();
        if (opts.background && now < toastHoldUntil) return;
        var toast = document.getElementById('toast');
        toast.textContent = message;
        toast.className = 'toast toast-' + (type || 'info');
        toast.style.display = 'block';
        if (!opts.background) { toastHoldUntil = now + 1500; }
        if (toastTimer) { clearTimeout(toastTimer); }
        toastTimer = setTimeout(function() { toast.style.display = 'none'; }, 3000);
    }
    function timeAgo(dateStr) {
        var then = new Date(dateStr), now = new Date(), seconds = Math.floor((now - then) / 1000);
        if (seconds < 60) return seconds + 's ago';
        var minutes = Math.floor(seconds / 60);
        if (minutes < 60) return minutes + 'm ago';
        var hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + 'h ago';
        return Math.floor(hours / 24) + 'd ago';
    }
    function timeUntil(dateStr) {
        var then = new Date(dateStr), now = new Date(), seconds = Math.max(0, Math.floor((then - now) / 1000));
        if (seconds < 60) return seconds + 's';
        var minutes = Math.floor(seconds / 60);
        if (minutes < 60) return minutes + 'm';
        var hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + 'h';
        return Math.floor(hours / 24) + 'd';
    }
    function updateColumnCount(stageId, delta) {
        var badge = document.getElementById('count-stage-' + stageId);
        if (badge) badge.textContent = parseInt(badge.textContent) + delta;
    }
    function clearPlaceholder(dropZone) {
        var ph = dropZone.querySelector('.kanban-empty-placeholder');
        if (ph) ph.remove();
    }
    function addPlaceholderIfEmpty(dropZone) {
        if (dropZone.querySelectorAll('.kanban-task-revamp').length === 0) {
            var div = document.createElement('div');
            div.className = 'kanban-empty-placeholder text-center text-secondary';
            div.innerHTML = '<small>\u2728 Ready for new tasks</small><small style="font-size: 0.7rem; margin-top: 0.5rem; opacity: 0.7;">Drop here</small>';
            dropZone.appendChild(div);
        }
    }

    function closeFolderPicker() { closeModal('folder-picker-modal'); }
    function openFolderPicker(targetInputId) {
        folderPickerTarget = document.getElementById(targetInputId);
        openModal('folder-picker-modal');
        loadFolder(folderPickerTarget.value || '');
    }
    function loadFolderFromInput() { loadFolder(document.getElementById('folder-picker-current').value); }
    function loadFolder(path) {
        var url = '/ui/api/folders' + (path ? '?path=' + encodeURIComponent(path) : '');
        document.getElementById('folder-picker-list').innerHTML = '<p class="text-secondary">Loading...</p>';
        apiFetch(url, {}, 'Failed to load folders')
            .then(function(data) {
                folderPickerCurrent = data.path;
                folderPickerParent = data.parent;
                folderPickerHome = data.home;
                document.getElementById('folder-picker-current').value = data.path;
                var list = document.getElementById('folder-picker-list');
                if (!data.folders.length) { list.innerHTML = '<p class="text-secondary">No folders found here.</p>'; return; }
                list.innerHTML = data.folders.map(function(folder) {
                    return '<button type="button" class="entity-item" style="width:100%;text-align:left;border:0;background:transparent;cursor:pointer;" onclick="loadFolder(' + escapeHtml(JSON.stringify(folder.path)) + ')"><strong>' + escapeHtml(folder.name) + '</strong><div class="text-secondary"><small>' + escapeHtml(folder.path) + '</small></div></button>';
                }).join('');
            })
            .catch(function(err) { document.getElementById('folder-picker-list').innerHTML = '<p class="text-danger">' + escapeHtml(err.message) + '</p>'; });
    }
    function selectCurrentFolder() { if (folderPickerTarget) folderPickerTarget.value = folderPickerCurrent; closeFolderPicker(); }
    function escapeHtml(value) {
        return String(value || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
    }
    var STATUS_LABELS = {
        pending: 'Pending',
        in_progress: 'In progress',
        in_review: 'In review',
        completed: 'Completed',
        blocked: 'Blocked'
    };
    function statusLabel(value) {
        return STATUS_LABELS[value] || String(value || '').replace(/_/g, ' ');
    }

    // --- Card action overflow menu ---
    window.closeTaskMenus = function(returnFocus) {
        document.querySelectorAll('.task-menu').forEach(function(menu) {
            menu.style.display = 'none';
        });
        document.querySelectorAll('.task-menu-btn[aria-expanded="true"]').forEach(function(btn) {
            btn.setAttribute('aria-expanded', 'false');
            if (returnFocus) btn.focus();
        });
    };
    window.toggleTaskMenu = function(taskId, btn) {
        var menu = document.getElementById('task-menu-' + taskId);
        if (!menu) return;
        var wasOpen = menu.style.display !== 'none';
        window.closeTaskMenus(false);
        if (wasOpen) return;
        menu.style.display = 'block';
        btn.setAttribute('aria-expanded', 'true');
        var firstItem = menu.querySelector('.task-menu-item');
        if (firstItem) firstItem.focus();
    };
    window.deleteTaskFromMenu = function(taskId) {
        window.closeTaskMenus(false);
        var card = document.getElementById('task-card-' + taskId);
        if (card) window.deleteTask(taskId, card);
    };
    document.addEventListener('click', function(e) {
        if (!e.target.closest || !e.target.closest('.task-menu-wrap')) window.closeTaskMenus(false);
    });
    document.addEventListener('keydown', function(e) {
        if (e.key !== 'Escape') return;
        if (document.querySelector('.task-menu-btn[aria-expanded="true"]')) {
            e.preventDefault();
            window.closeTaskMenus(true);
        }
    });

    // --- Drag and Drop ---
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
    function moveTaskCard(card, targetZone) {
        if (!card || !targetZone || card.dataset.moving === 'true') return Promise.resolve(false);
        var taskId = card.dataset.taskId;
        var oldStageId = card.dataset.currentStage;
        var newStageId = targetZone.dataset.stageId;
        if (oldStageId === newStageId) return Promise.resolve(false);
        var column = targetZone.closest('.kanban-column-revamp');
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
        return apiFetch('/ui/tasks/' + taskId + '/move', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID },
            body: JSON.stringify({ stage_id: parseInt(newStageId), status: newStatus, summary: '' })
        }, 'Failed to update task').then(function() {
            card.setAttribute('aria-label', 'Task ' +
                (card.querySelector('.task-title-revamp') || {}).textContent + ' in ' + stageName +
                '. Use left and right arrow keys to move.');
            showToast('Task moved to ' + stageName, 'success');
            return true;
        }).catch(function(err) {
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
            showToast('Failed to move task: ' + err.message, 'error');
            return false;
        }).finally(function() {
            delete card.dataset.moving;
            card.focus();
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

    // --- Move a Backlog card to To Do ---
    window.moveToTodo = function(taskId, btn) {
        var todoCol = document.querySelector('.kanban-column-revamp[data-stage-key="to_do"]');
        if (!todoCol) { showToast('No "To Do" column found', 'error'); return; }
        var todoZone = todoCol.querySelector('.kanban-drop-zone-revamp');
        var todoStageId = todoZone.dataset.stageId;
        var card = document.getElementById('task-card-' + taskId);
        var oldZone = card.parentElement;
        var oldStageId = card.dataset.currentStage;
        if (oldStageId === todoStageId) return;
        btn.disabled = true;
        apiFetch('/ui/tasks/' + taskId + '/move', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID },
            body: JSON.stringify({ stage_id: parseInt(todoStageId), status: 'pending', summary: 'Moved from Backlog to To Do' })
        }, 'Failed to move task').then(function() {
            clearPlaceholder(todoZone);
            todoZone.appendChild(card);
            card.dataset.currentStage = todoStageId;
            addPlaceholderIfEmpty(oldZone);
            updateColumnCount(oldStageId, -1);
            updateColumnCount(todoStageId, 1);
            btn.remove();
            showToast('Task moved to To Do', 'success');
        }).catch(function(err) {
            btn.disabled = false;
            showToast('Failed: ' + err.message, 'error');
        });
    };

    // --- Task CRUD ---
    function setTaskFormError(message) {
        var el = document.getElementById('task-form-error');
        if (!el) return;
        el.textContent = message || '';
        el.style.display = message ? '' : 'none';
    }
    window.openAddTaskModal = function(stageId, stageName) {
        document.getElementById('task-modal-title').textContent = 'New task';
        document.getElementById('task-form-mode').value = 'create';
        document.getElementById('task-form-id').value = '';
        var stageSelect = document.getElementById('task-form-stage');
        if (stageId) stageSelect.value = String(stageId);
        else if (PLAN_STAGE_ID) stageSelect.value = String(PLAN_STAGE_ID);
        else stageSelect.selectedIndex = 0;
        stageSelect.disabled = false;
        document.getElementById('task-form-stage-hint').style.display = 'none';
        document.getElementById('task-form-title').value = '';
        document.getElementById('task-form-desc').value = '';
        document.getElementById('task-form-priority').value = '0';
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
        var stageSelect = document.getElementById('task-form-stage');
        if (taskEl.dataset.currentStage) stageSelect.value = taskEl.dataset.currentStage;
        // The edit endpoint does not move cards; the board does.
        stageSelect.disabled = true;
        document.getElementById('task-form-stage-hint').style.display = '';
        var title = taskEl.querySelector('.task-title-revamp');
        document.getElementById('task-form-title').value = title ? title.textContent.trim() : '';
        document.getElementById('task-form-desc').value = '';
        document.getElementById('task-form-status').value = 'pending';
        document.getElementById('task-form-priority').value = '0';
        document.getElementById('task-form-skills').value = '';
        document.getElementById('task-form-submit').textContent = 'Save changes';
        setTaskFormError('');
        var commentsList = document.getElementById('task-comments-list');
        commentsList.innerHTML = '<p class="text-secondary">Loading activity...</p>';
        document.getElementById('task-comments-section').style.display = 'block';
        apiFetch('/tasks/' + taskId, {}, 'Failed to load task details').then(function(data) {
            document.getElementById('task-form-title').value = data.title || '';
            document.getElementById('task-form-desc').value = data.description || '';
            document.getElementById('task-form-status').value = data.status || 'pending';
            if (data.priority !== undefined) document.getElementById('task-form-priority').value = data.priority;
            document.getElementById('task-form-skills').value = data.required_skills || '';
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
            commentsList.innerHTML = '<p class="text-danger">' + escapeHtml(err.message) + '</p>';
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
                board.replaceWith(freshBoard);
                initDragDrop();
                initExpandedCards();
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
                return true;
            }).catch(function(error) {
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
            request = apiFetch('/ui/tasks/' + taskId + '/edit', { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID }, body: JSON.stringify(data) }, 'Failed to update task')
                .then(function() { showToast('Task updated!', 'success'); closeModal('task-modal'); return refreshBoardFromServer(); })
                .catch(function(err) { setTaskFormError(err.message); showToast('Error: ' + err.message, 'error'); });
        }
        request.finally(function() {
            taskFormPending = false;
            if (btn) { btn.disabled = false; }
        });
    };
    window.deleteTask = function(taskId, taskEl) {
        if (!confirm('Delete this task?')) return;
        var stageId = taskEl.dataset.currentStage;
        apiFetch('/ui/tasks/' + taskId, { method: 'DELETE', headers: { 'x-entity-id': CURRENT_ENTITY_ID } }, 'Failed to delete task')
            .then(function() { var dropZone = taskEl.parentElement; taskEl.remove(); addPlaceholderIfEmpty(dropZone); updateColumnCount(stageId, -1); showToast('Task deleted', 'success'); })
            .catch(function(err) { showToast('Error: ' + err.message, 'error'); });
    };

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
        var pathBtn = document.querySelector('#project-modal button[onclick*="openFolderPicker"]');
        if (pathBtn) pathBtn.focus();
    };
    window.assignRoleToTask = function(taskId, roleName, btn) {
        btn.disabled = true; btn.textContent = '...';
        apiFetch('/ui/tasks/' + taskId + '/assign-role', { method: 'POST', headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID }, body: JSON.stringify({ role: roleName }) }, 'Failed to assign role')
            .then(function() { showToast('Assigned to ' + roleName, 'success'); closeModal('assign-modal'); return refreshBoardFromServer(); })
            .catch(function(err) { showToast('Error: ' + err.message, 'error'); btn.disabled = false; btn.textContent = 'Assign'; });
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

    // --- Plan work (chat task creation) ---
    var chatInput = document.getElementById('chat-task-input');
    if (chatInput) {
        chatInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); createTaskFromChat(); }
        });
    }
    var planRequestPending = false;
    function setPlanError(message) {
        var el = document.getElementById('chat-plan-error');
        if (!el) return;
        el.textContent = message || '';
        el.style.display = message ? '' : 'none';
    }
    async function createTaskFromChat() {
        var input = document.getElementById('chat-task-input');
        var btn = document.getElementById('chat-plan-btn');
        if (!input || planRequestPending) return;
        var text = input.value.trim();
        if (!text) { setPlanError('Describe the work you want to plan.'); input.focus(); return; }
        setPlanError('');
        planRequestPending = true;
        if (btn) { btn.disabled = true; btn.textContent = 'Planning\u2026'; }
        try {
            var resp = await fetch('/ui/tasks/chat-plan', { method: 'POST', headers: {'Content-Type': 'application/json', 'X-Entity-ID': CURRENT_ENTITY_ID || ''}, body: JSON.stringify({ project_id: PROJECT_ID, message: text }) });
            if (!resp.ok) { var error = await resp.json().catch(function() { return {}; }); throw new Error(error.detail || 'Failed to create the plan'); }
            var data = await resp.json();
            input.value = '';
            var wrap = document.getElementById('plan-work-wrap');
            var stageName = wrap && wrap.dataset.stageName ? wrap.dataset.stageName : 'Backlog';
            showToast('Created ' + data.tasks.length + ' card' + (data.tasks.length === 1 ? '' : 's') + ' in ' + stageName, 'success');
            refreshBoardFromServer();
        } catch(err) {
            // Keep the entered text so the request can be retried after fixing
            // the reported problem.
            setPlanError(err.message);
            input.focus();
        } finally {
            planRequestPending = false;
            if (btn) { btn.disabled = false; btn.textContent = 'Plan work'; }
        }
    }

    // --- WebSocket ---
    var boardWsBackoff = 1000;
    var BOARD_WS_MAX_BACKOFF = 30000;
    function initWebSocket() {
        var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var wsUrl = protocol + '//' + window.location.host + '/ws/projects/' + PROJECT_ID;
        console.log('Connecting to project updates:', wsUrl);
        var socket = new WebSocket(wsUrl);
        socket.onopen = function() { boardWsBackoff = 1000; refreshBoardFromServer(); };
        socket.onmessage = function(event) {
            var msg = JSON.parse(event.data);
            console.log('WS Message:', msg);
            if (msg.event_type === 'task_updated' || msg.event_type === 'task_moved') {
                var data = msg.data;
                var label = data.status ? statusLabel(data.status) : 'Updated';
                showToast('Task #' + data.task_id + ': ' + label, 'info', {background: true});
                var badge = document.getElementById('status-task-' + data.task_id);
                if (badge && data.status) {
                    badge.className = 'badge badge-' + data.status; badge.textContent = label;
                    badge.closest('.kanban-task-revamp').dataset.status = data.status;
                }
                if (msg.event_type === 'task_moved' || data.status === 'completed') { setTimeout(refreshBoardFromServer, 500); }
            } else if (msg.event_type === 'task_commented') {
                showToast('\u{1F4AC} Task #' + msg.data.task_id + ': ' + msg.data.comment.substring(0, 30) + '...', 'info', {background: true});
            } else if (msg.event_type === 'task_assigned') {
                showToast('\u{1F464} Task #' + msg.data.task_id + ' assigned', 'info', {background: true});
                setTimeout(refreshBoardFromServer, 500);
            } else if (msg.event_type === 'agent_status_updated') {
                var data = msg.data;
                // Update card session indicator
                if (data.task_id) updateCardSessionIndicator(data.task_id, data.status_type === 'working' || data.status_type === 'thinking' ? {id: data.session_id, agent_id: data.agent_id, status: data.status_type} : null);
            } else if (msg.event_type === 'agent_activity_logged') {
                var data = msg.data;
                // Add recent-activity dot on card
                if (data.task_id) {
                    var card = document.getElementById('task-card-' + data.task_id);
                    if (card) {
                        var indicators = card.querySelector('.task-state-indicators');
                        var existingDot = indicators ? indicators.querySelector('.task-indicator.recent-activity') : null;
                        if (indicators && !existingDot) {
                            var dot = document.createElement('span');
                            dot.className = 'task-indicator recent-activity';
                            dot.title = 'Recent activity';
                            indicators.appendChild(dot);
                        }
                    }
                }
            }
            if (msg.event_type === 'diff_review_requested' || msg.event_type === 'diff_review_completed') {
                // Refresh in-card reviews for expanded cards
                document.querySelectorAll('[id^="task-reviews-list-"]').forEach(function(el) {
                    var taskId = el.id.replace('task-reviews-list-', '');
                    fetchTaskReviews(parseInt(taskId));
                });
            }
            if (msg.event_type === 'agent_approval_requested') {
                openApprovalPopupFromEvent(msg.data);
                showToast('Approval needed: ' + (msg.data.title || msg.data.approval_type), 'warning');
                fetchAgentApprovals();
                sendOSNotification('Approval Requested', msg.data.title || msg.data.approval_type, msg.data.task_id);
                // Toast should scroll to the card
                var toastEl = document.getElementById('toast');
                if (toastEl) {
                    var linkBtn = document.createElement('button');
                    linkBtn.textContent = 'Review';
                    linkBtn.className = 'btn btn-sm';
                    linkBtn.style.marginLeft = '0.5rem';
                    linkBtn.onclick = function() { openApprovalPopupFromEvent(msg.data); };
                    toastEl.appendChild(linkBtn);
                }
            } else if (msg.event_type === 'agent_approval_resolved') {
                showToast('Approval #' + msg.data.approval_id + ' ' + msg.data.status, 'info');
                fetchAgentApprovals();
                if (msg.data.task_id) fetchTaskApprovals(msg.data.task_id);
                if (currentApprovalId && String(currentApprovalId) === String(msg.data.approval_id)) closeApprovalPopup();
            }
        };
        socket.onclose = function() { console.log('WS Connection closed. Retrying in ' + boardWsBackoff + 'ms...'); setTimeout(initWebSocket, boardWsBackoff); boardWsBackoff = Math.min(boardWsBackoff * 2, BOARD_WS_MAX_BACKOFF); };
        socket.onerror = function(err) { console.error('WS Error:', err); };
    }

    // --- Initialize ---
    initDragDrop();
    initWebSocket();
    initExpandedCards();
    initNotificationSettings();
    fetchAgentApprovals();
    // Approvals refresh via WebSocket events only (no polling)
