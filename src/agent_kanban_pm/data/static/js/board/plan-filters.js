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
    window.openPlanWorkModal = function() {
        setPlanError('');
        openModal('plan-work-modal');
        var input = document.getElementById('chat-task-input');
        if (input) window.setTimeout(function() { input.focus(); }, 0);
    };
    var planPreviewRequest = null;
    function updatePlanPreviewCount() {
        var rows = Array.from(document.querySelectorAll('#plan-preview-items .plan-preview-row'));
        var count = rows.filter(function(row) { return row.querySelector('.plan-include').checked; }).length;
        document.getElementById('plan-preview-count').textContent = count + ' selected task' + (count === 1 ? '' : 's');
        document.getElementById('plan-preview-create').disabled = !count || planRequestPending;
    }
    function renderPlanPreview(items) {
        var list = document.getElementById('plan-preview-items');
        list.replaceChildren();
        items.forEach(function(item, index) {
            var row = document.createElement('fieldset');
            row.className = 'plan-preview-row';
            var legend = document.createElement('legend');
            legend.textContent = 'Proposed task ' + (index + 1);
            row.appendChild(legend);
            var includeLabel = document.createElement('label');
            var checkbox = document.createElement('input');
            checkbox.type = 'checkbox'; checkbox.className = 'plan-include'; checkbox.checked = true;
            checkbox.addEventListener('change', updatePlanPreviewCount);
            includeLabel.append(checkbox, document.createTextNode(' Create this task'));
            row.appendChild(includeLabel);
            [['Title', 'text', item.title], ['Description', 'textarea', item.description || ''], ['Priority (0–10)', 'number', item.priority]].forEach(function(field) {
                var label = document.createElement('label');
                label.textContent = field[0];
                var input = document.createElement(field[1] === 'textarea' ? 'textarea' : 'input');
                if (field[1] !== 'textarea') input.type = field[1];
                input.value = field[2];
                input.className = 'form-input plan-' + (field[0].startsWith('Priority') ? 'priority' : field[0].toLowerCase());
                if (field[1] === 'number') { input.min = 0; input.max = 10; }
                label.appendChild(input);
                row.appendChild(label);
            });
            var remove = document.createElement('button');
            remove.type = 'button'; remove.className = 'btn btn-link';
            remove.textContent = 'Remove proposal';
            remove.addEventListener('click', function() { row.remove(); updatePlanPreviewCount(); });
            row.appendChild(remove);
            list.appendChild(row);
        });
        document.getElementById('plan-preview-error').textContent = '';
        updatePlanPreviewCount();
        closeModal('plan-work-modal');
        openModal('plan-preview-modal');
        var first = list.querySelector('.plan-title');
        if (first) first.focus();
    }
    function closePlanPreview(returnToPlanner) {
        if (planRequestPending) return;
        closeModal('plan-preview-modal');
        planPreviewRequest = null;
        if (returnToPlanner !== false) {
            openPlanWorkModal();
            var input = document.getElementById('chat-task-input');
            if (input) window.setTimeout(function() { input.focus(); }, 0);
        }
    }
    async function createTaskFromChat() {
        var input = document.getElementById('chat-task-input');
        var btn = document.getElementById('chat-plan-btn');
        if (!input || planRequestPending) return;
        var text = input.value.trim();
        if (!text) { setPlanError('Describe the work you want to plan.'); input.focus(); return; }
        setPlanError('');
        planRequestPending = true;
        if (btn) { btn.disabled = true; btn.textContent = 'Preparing…'; }
        try {
            var resp = await fetch('/ui/tasks/chat-plan/preview', { method: 'POST', headers: {'Content-Type': 'application/json', 'X-Entity-ID': CURRENT_ENTITY_ID || ''}, body: JSON.stringify({ project_id: PROJECT_ID, message: text }) });
            var data = await resp.json().catch(function() { return {}; });
            if (!resp.ok) throw new Error(data.detail || 'Could not prepare the plan');
            planPreviewRequest = text;
            renderPlanPreview(data.items || []);
        } catch(err) {
            setPlanError(err.message);
            input.focus();
        } finally {
            planRequestPending = false;
            if (btn) { btn.disabled = false; btn.textContent = 'Preview plan'; }
            updatePlanPreviewCount();
        }
    }
    async function commitPlanPreview() {
        if (planRequestPending || !planPreviewRequest) return;
        var items = [];
        var error = document.getElementById('plan-preview-error');
        Array.from(document.querySelectorAll('#plan-preview-items .plan-preview-row')).forEach(function(row) {
            if (!row.querySelector('.plan-include').checked) return;
            items.push({
                title: row.querySelector('.plan-title').value.trim(),
                description: row.querySelector('.plan-description').value,
                priority: Number(row.querySelector('.plan-priority').value)
            });
        });
        if (!items.length) { error.textContent = 'Select at least one task.'; return; }
        if (items.some(function(item) { return !item.title || !Number.isInteger(item.priority) || item.priority < 0 || item.priority > 10; })) {
            error.textContent = 'Give each selected task a title and a priority from 0 to 10.'; return;
        }
        planRequestPending = true;
        var button = document.getElementById('plan-preview-create');
        button.disabled = true; button.textContent = 'Creating…';
        error.textContent = '';
        try {
            var resp = await fetch('/ui/tasks/chat-plan', { method: 'POST', headers: {'Content-Type': 'application/json', 'X-Entity-ID': CURRENT_ENTITY_ID || ''}, body: JSON.stringify({ project_id: PROJECT_ID, message: planPreviewRequest, items: items }) });
            var data = await resp.json().catch(function() { return {}; });
            if (!resp.ok) throw new Error(data.detail || 'Could not create tasks');
            planRequestPending = false;
            closePlanPreview(false);
            document.getElementById('chat-task-input').value = '';
            var wrap = document.getElementById('plan-work-wrap');
            var stageName = wrap && wrap.dataset.stageName ? wrap.dataset.stageName : 'Backlog';
            showToast('Created ' + data.tasks.length + ' card' + (data.tasks.length === 1 ? '' : 's') + ' in ' + stageName, 'success');
            refreshBoardFromServer();
        } catch(err) {
            error.textContent = err.message;
            planRequestPending = false;
        } finally {
            button.textContent = 'Create selected tasks';
            updatePlanPreviewCount();
        }
    }

    // --- Board search and attention filters ---
    function updateAgentFilterChoices() {
        var select = document.getElementById('board-agent-filter');
        if (!select) return;
        var selected = select.value;
        var agents = new Map();
        document.querySelectorAll('.kanban-task-revamp .badge-assignee.badge-agent').forEach(function(badge) {
            agents.set(badge.dataset.entityId, (badge.title || '').replace(/ \(agent\)$/, ''));
        });
        select.replaceChildren(new Option('All agents', ''));
        Array.from(agents.entries()).sort(function(a, b) { return a[1].localeCompare(b[1]); })
            .forEach(function(entry) { select.add(new Option(entry[1], entry[0])); });
        if (selected && !agents.has(selected)) select.add(new Option('Previously selected agent', selected));
        select.value = selected;
    }
    function initBoardFilters() {
        var url = new URL(window.location.href);
        document.getElementById('board-search').value = url.searchParams.get('q') || '';
        document.getElementById('board-filter').value = url.searchParams.get('view') || '';
        document.getElementById('board-priority-filter').value = url.searchParams.get('priority') || '';
        updateAgentFilterChoices();
        var agent = url.searchParams.get('agent') || '';
        if (agent && !Array.from(document.getElementById('board-agent-filter').options).some(function(option) { return option.value === agent; })) {
            document.getElementById('board-agent-filter').add(new Option('Selected agent', agent));
        }
        document.getElementById('board-agent-filter').value = agent;
        applyBoardFilters(true);
    }
    function applyBoardFilters(skipUrlUpdate) {
        var search = document.getElementById('board-search');
        var modeSelect = document.getElementById('board-filter');
        var agentSelect = document.getElementById('board-agent-filter');
        var prioritySelect = document.getElementById('board-priority-filter');
        if (!search || !modeSelect || !agentSelect || !prioritySelect) return;
        var query = search.value.trim().toLowerCase();
        var mode = modeSelect.value;
        var agentId = agentSelect.value;
        var priority = prioritySelect.value;
        var active = !!(query || mode || agentId || priority);
        var cards = Array.from(document.querySelectorAll('.kanban-task-revamp'));
        var matching = 0;
        cards.forEach(function(card) {
            var id = card.dataset.taskId;
            var title = (card.querySelector('.task-title-revamp') || {}).textContent || '';
            var value = Number(card.dataset.priorityValue || 0);
            var priorityGroup = value <= 0 ? 'none' : value <= 3 ? 'low' :
                value <= 6 ? 'normal' : value <= 8 ? 'high' : 'urgent';
            var execution = card.dataset.execution || '';
            var hasAgent = !!card.querySelector('.badge-assignee.badge-agent');
            var matchesMode = !mode ||
                (mode === 'needs-me' && card.dataset.needsMe === 'true') ||
                (mode === 'blocked' && (card.dataset.status === 'blocked' ||
                    execution === 'blocked' || execution === 'error')) ||
                (mode === 'running' && (execution === 'starting' || execution === 'active')) ||
                (mode === 'unassigned' && !hasAgent);
            var matchesAgent = !agentId || Array.from(card.querySelectorAll('.badge-assignee.badge-agent')).some(function(badge) {
                return badge.dataset.entityId === agentId;
            });
            var visible = (!query || title.toLowerCase().includes(query) || String(id) === query ||
                ('#' + id) === query) && matchesMode && matchesAgent &&
                (!priority || priority === priorityGroup);
            card.classList.toggle('is-filtered-out', !visible);
            if (visible) matching++;
        });
        document.getElementById('board-match-count').textContent =
            cards.length ? matching + ' of ' + cards.length + ' tasks' : 'No tasks yet';
        document.getElementById('board-clear-filters').hidden = !active;
        document.getElementById('board-filter-empty').hidden = !(active && cards.length && !matching);
        var board = document.getElementById('board-main-revamp');
        if (board) board.classList.toggle('board-has-filters', active);
        if (!skipUrlUpdate) {
            var url = new URL(window.location.href);
            [['q', query], ['view', mode], ['agent', agentId], ['priority', priority]].forEach(function(entry) {
                if (entry[1]) url.searchParams.set(entry[0], entry[1]);
                else url.searchParams.delete(entry[0]);
            });
            history.replaceState(history.state || {}, '', url);
        }
    }
    function clearBoardFilters() {
        document.getElementById('board-search').value = '';
        document.getElementById('board-filter').value = '';
        document.getElementById('board-agent-filter').value = '';
        document.getElementById('board-priority-filter').value = '';
        applyBoardFilters();
        document.getElementById('board-search').focus();
    }
