    // --- WebSocket ---
    var boardWsBackoff = 1000;
    var boardSocket = null;
    var boardWsNeedsRefresh = false;
    var BOARD_WS_MAX_BACKOFF = 30000;
    var boardExecutionRefreshTimer = null;
    var ACTIVITY_TYPE_LABELS = {
        thought: 'Thought', action: 'Action', observation: 'Output', result: 'Result',
        error: 'Error', file_change: 'File change', command: 'Command',
        tool_call: 'Tool call', handoff: 'Handoff'
    };
    var LOW_SIGNAL_ACTIVITY_TYPES = {thought: true, observation: true};
    function normalizeActivityPreview(message) {
        var text = String(message || '')
            .replace(/[\u001B\u009B][[\]()#;?]*(?:(?:(?:[a-zA-Z\d]*(?:;[-a-zA-Z\d\/#&.:=?%@~_]+)*)?\u0007)|(?:(?:\d{1,4}(?:[;:]\d{0,4})*)?[\dA-PR-TZcf-nq-uy=><~]))/g, '')
            .replace(/[\u0000-\u0008\u000B-\u001F\u007F-\u009F]/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
        return text.length > 180 ? text.slice(0, 179).trimEnd() + '\u2026' : text;
    }
    function updateTaskLiveActivity(data, eventTimestamp) {
        if (!data || !data.task_id) return;
        var summary = document.getElementById('task-live-summary-' + data.task_id);
        if (!summary) return;
        var nextMessage = normalizeActivityPreview(data.message) || 'Activity recorded';
        var nextId = Number(data.activity_id || 0);
        var currentId = Number(summary.dataset.activityId || 0);
        var nextAt = eventTimestamp || data.created_at || new Date().toISOString();
        var currentAt = summary.dataset.activityAt || '';
        var type = data.activity_type || data.type || 'action';
        var currentType = summary.dataset.activityType || '';
        if (LOW_SIGNAL_ACTIVITY_TYPES[type] && currentType && !LOW_SIGNAL_ACTIVITY_TYPES[currentType]) return;
        if (nextId && currentId && nextId <= currentId) return;
        if ((!nextId || !currentId) && currentAt && Date.parse(nextAt) < Date.parse(currentAt)) return;
        var label = ACTIVITY_TYPE_LABELS[type] || String(type).replace(/_/g, ' ');
        label = label.charAt(0).toUpperCase() + label.slice(1);
        var agentLabel = data.agent_name || (data.agent_id ? 'Agent #' + data.agent_id : 'Agent');
        var typeEl = summary.querySelector('.task-live-type');
        var messageEl = summary.querySelector('.task-live-message');
        if (typeEl) typeEl.textContent = label;
        if (messageEl) messageEl.textContent = nextMessage;
        summary.dataset.activityId = nextId ? String(nextId) : '';
        summary.dataset.activityAt = nextAt;
        summary.dataset.activityType = type;
        summary.title = 'Latest activity from ' + agentLabel;
        summary.setAttribute('aria-label', 'Latest activity from ' + agentLabel + ': ' + label + '. ' + nextMessage);
        summary.hidden = false;
    }
    function setBoardConnectionStatus(message, stale) {
        var status = document.getElementById('board-connection-status');
        if (!status) return;
        status.textContent = message;
        status.classList.toggle('is-stale', !!stale);
    }
    function initWebSocket() {
        var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var wsUrl = protocol + '//' + window.location.host + '/ws/projects/' + PROJECT_ID;
        console.log('Connecting to project updates:', wsUrl);
        var socket = new WebSocket(wsUrl);
        boardSocket = socket;
        socket.onopen = function() {
            boardWsBackoff = 1000;
            // The server-rendered board is current on first load. Only a
            // reconnect needs a fetch to recover events missed while offline.
            if (!boardWsNeedsRefresh) {
                setBoardConnectionStatus('Live', false);
                return;
            }
            setBoardConnectionStatus('Refreshing…', true);
            refreshBoardFromServer().then(function(ok) {
                if (socket.readyState !== WebSocket.OPEN) return;
                if (ok) boardWsNeedsRefresh = false;
                setBoardConnectionStatus(ok ? 'Live' : 'Refresh failed', !ok);
            });
        };
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
                if (activeTaskId === Number(data.task_id)) fetchTaskOverview(activeTaskId);
                var isExpectedLocalMove = msg.event_type === 'task_moved' && consumeLocalMoveEvent(data);
                if (!isExpectedLocalMove && (msg.event_type === 'task_moved' || data.status === 'completed')) {
                    setTimeout(refreshBoardFromServer, 500);
                }
            } else if (msg.event_type === 'task_commented') {
                showToast('\u{1F4AC} Task #' + msg.data.task_id + ': ' + msg.data.comment.substring(0, 30) + '...', 'info', {background: true});
            } else if (msg.event_type === 'task_assigned') {
                showToast('\u{1F464} Task #' + msg.data.task_id + ' assigned', 'info', {background: true});
                setTimeout(refreshBoardFromServer, 500);
            } else if (msg.event_type === 'agent_status_updated') {
                var data = msg.data;
                // Update card session indicator
                if (data.task_id) {
                    updateCardSessionIndicator(data.task_id, data.status_type === 'working' || data.status_type === 'thinking' ? {id: data.session_id, agent_id: data.agent_id, status: data.status_type} : null);
                    if (activeTaskId === Number(data.task_id)) fetchTaskOverview(activeTaskId);
                    if (!boardExecutionRefreshTimer) boardExecutionRefreshTimer = setTimeout(function() {
                        boardExecutionRefreshTimer = null;
                        refreshBoardFromServer();
                    }, 700);
                }
            } else if (msg.event_type === 'agent_activity_logged') {
                var data = msg.data;
                // Add recent-activity dot on card
                if (data.task_id) {
                    updateTaskLiveActivity(data, msg.timestamp);
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
                    if (activeTaskId === Number(data.task_id) &&
                        expandedCardTabs[activeTaskId] === 'terminal' && !boardTerminalRefreshTimer) {
                        boardTerminalRefreshTimer = setTimeout(function() {
                            boardTerminalRefreshTimer = null;
                            if (activeTaskId === Number(data.task_id)) fetchTaskTerminal(activeTaskId);
                        }, 300);
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
                // Durable events may predate this page. Existing approvals
                // belong in the bell queue, without taking focus on page load.
                if (!msg.timestamp || Date.parse(msg.timestamp) >= boardLoadedAt) {
                    openApprovalPopupFromEvent(msg.data);
                }
                showToast('Approval needed: ' + (msg.data.title || msg.data.approval_type), 'warning');
                fetchAgentApprovals();
                setTimeout(refreshBoardFromServer, 300);
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
                showToast('Approval ' + msg.data.status + ' (#' + msg.data.approval_id + ')', 'info');
                fetchAgentApprovals();
                setTimeout(refreshBoardFromServer, 300);
                if (msg.data.task_id) fetchTaskApprovals(msg.data.task_id);
                if (activeTaskId === Number(msg.data.task_id)) fetchTaskOverview(activeTaskId);
                if (currentApprovalId && String(currentApprovalId) === String(msg.data.approval_id)) closeApprovalPopup();
            }
        };
        socket.onclose = function() { boardWsNeedsRefresh = true; setBoardConnectionStatus('Reconnecting…', true); console.log('WS Connection closed. Retrying in ' + boardWsBackoff + 'ms...'); setTimeout(initWebSocket, boardWsBackoff); boardWsBackoff = Math.min(boardWsBackoff * 2, BOARD_WS_MAX_BACKOFF); };
        socket.onerror = function(err) { setBoardConnectionStatus('Connection issue', true); console.error('WS Error:', err); };
    }
