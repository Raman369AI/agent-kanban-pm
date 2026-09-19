function escapeHtml(value) {
        var div = document.createElement('div');
        div.textContent = value == null ? '' : String(value);
        return div.innerHTML;
    }
    function showToast(message, type) {
        var toast = document.getElementById('toast');
        toast.textContent = message;
        toast.className = 'toast toast-' + (type || 'info');
        toast.style.display = 'block';
        setTimeout(function () { toast.style.display = 'none'; }, 3000);
    }

    function submitCreateProject(e) {
        e.preventDefault();
        if (!CURRENT_ENTITY_ID) {
            showToast('Authentication required. Please set X-Entity-ID header.', 'error');
            return;
        }
        var form = e.target;
        var data = {
            name: form.name.value.trim(),
            description: form.description.value.trim(),
            path: form.path.value.trim()
        };
        if (!data.name) { showToast('Project name is required', 'error'); return; }

        apiFetch('/ui/projects/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID },
            body: JSON.stringify(data)
        }, 'Failed to create project')
        .then(function (project) {
            window.location.href = '/ui/projects/' + project.id + '/board';
        })
        .catch(function (err) { showToast('Error: ' + err.message, 'error'); });
    }

    // Wire up the form
    document.addEventListener('DOMContentLoaded', function () {
        var form = document.querySelector('#create-modal form');
        if (form) form.addEventListener('submit', submitCreateProject);
    });

    function openProjectCard(event, card) {
        if (event.defaultPrevented || event.target.closest('a, button, input, textarea, select, details, summary')) return;
        window.location.href = card.dataset.boardUrl;
    }

    async function deleteProject(projectId) {
        var confirmed = await confirmAction({
            title: 'Delete project?',
            message: 'This permanently deletes the project, its tasks, history, and sessions.',
            confirmLabel: 'Delete project',
            danger: true
        });
        if (!confirmed) return;
        if (!CURRENT_ENTITY_ID) {
            showToast('Authentication required', 'error');
            return;
        }
        apiFetch(
            '/ui/projects/' + projectId,
            { method: 'DELETE', headers: { 'x-entity-id': CURRENT_ENTITY_ID } },
            'Failed to delete project'
        )
            .then(function () {
                var card = document.getElementById('project-card-' + projectId);
                if (card) card.remove();
                showToast('Project deleted', 'success');
            })
            .catch(function (err) { showToast('Error: ' + err.message, 'error'); });
    }

    function openProjectFolder(path) {
        fetch('/ui/api/open-workspace', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID || '' },
            body: JSON.stringify({ path: path }),
        }).then(async function (r) {
            const data = await r.json().catch(function () { return {}; });
            if (r.ok) {
                showToast('Opened ' + (data.path || path), 'success');
            } else {
                showToast('Could not open: ' + (data.detail || r.statusText), 'error');
            }
        }).catch(function (err) { showToast('Open failed: ' + err.message, 'error'); });
    }
