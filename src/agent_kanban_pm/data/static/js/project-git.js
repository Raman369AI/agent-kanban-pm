let allContributions = [];
    let currentFilter = 'all';

    function escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function timeAgo(dateString) {
        const date = new Date(dateString);
        const now = new Date();
        const seconds = Math.floor((now - date) / 1000);

        if (seconds < 60) return 'Just now';
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes}m ago`;
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}h ago`;
        const days = Math.floor(hours / 24);
        return `${days}d ago`;
    }

    function showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    async function fetchContributions() {
        try {
            const response = await fetch(`/agents/projects/${PROJECT_ID}/contributions?limit=200`);
            allContributions = await response.json();
            updateStats();
            renderContributions();
            updateLastSyncTime();
        } catch (error) {
            console.error('Error fetching contributions:', error);
            document.getElementById('contribution-container').innerHTML =
                '<div class="empty-state">Error loading contributions. Please try again later.</div>';
        }
    }

    function updateStats() {
        const stats = {
            open_prs: allContributions.filter(c => c.contribution_type === 'pull_request' && c.status === 'open').length,
            merged_prs: allContributions.filter(c => c.contribution_type === 'pull_request' && c.status === 'merged').length,
            open_issues: allContributions.filter(c => c.contribution_type === 'issue' && c.status === 'open').length,
            commits: allContributions.filter(c => c.contribution_type === 'commit').length
        };

        document.getElementById('stat-open-prs').textContent = stats.open_prs;
        document.getElementById('stat-merged-prs').textContent = stats.merged_prs;
        document.getElementById('stat-open-issues').textContent = stats.open_issues;
        document.getElementById('stat-commits').textContent = stats.commits;
    }

    function updateLastSyncTime() {
        if (allContributions.length === 0) {
            document.getElementById('last-sync-time').textContent = 'Never synced';
            return;
        }
        const latest = allContributions.reduce((prev, curr) =>
            new Date(curr.recorded_at) > new Date(prev.recorded_at) ? curr : prev
        );
        document.getElementById('last-sync-time').textContent = `Last synced: ${timeAgo(latest.recorded_at)}`;
    }

    function safeContributionUrl(value) {
        if (!value) return null;
        try {
            const parsed = new URL(value, window.location.origin);
            return parsed.protocol === 'https:' ? parsed.href : null;
        } catch (error) {
            return null;
        }
    }

    function renderContributions() {
        const container = document.getElementById('contribution-container');
        const filtered = currentFilter === 'all'
            ? allContributions
            : allContributions.filter(c => c.contribution_type === currentFilter);
        container.replaceChildren();

        if (filtered.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            const type = currentFilter === 'all'
                ? ''
                : currentFilter.replace('_', ' ') + 's ';
            empty.textContent = 'No ' + type + 'synced yet. Click Sync Now to pull data from GitHub.';
            container.append(empty);
            return;
        }

        filtered.forEach(item => {
            const iconText = item.contribution_type === 'pull_request'
                ? '🔀' : (item.contribution_type === 'issue' ? '🐛' : '📝');
            const typeLabel = String(item.contribution_type || '').replace('_', ' ').toUpperCase();
            let statusClass = 'badge-pending';
            if (item.status === 'open') statusClass = 'badge-success';
            else if (item.status === 'merged') statusClass = 'badge-merged';
            else if (item.status === 'closed') statusClass = 'badge-blocked';

            const card = document.createElement('div');
            card.className = 'contribution-card';
            const left = document.createElement('div');
            left.className = 'cont-left';
            const icon = document.createElement('div');
            icon.className = 'cont-icon';
            icon.textContent = iconText;
            const info = document.createElement('div');
            info.className = 'cont-info';

            const safeUrl = safeContributionUrl(item.url);
            const title = document.createElement(safeUrl ? 'a' : 'span');
            title.className = 'cont-title';
            title.textContent = item.title || 'Untitled contribution';
            if (safeUrl) {
                title.href = safeUrl;
                title.target = '_blank';
                title.rel = 'noopener noreferrer';
            }

            const externalId = document.createElement('div');
            externalId.className = 'cont-external-id';
            externalId.textContent = item.external_id || '';
            const meta = document.createElement('div');
            meta.className = 'cont-meta';
            meta.textContent = typeLabel + ' • ' + timeAgo(item.recorded_at);
            info.append(title, externalId, meta);
            left.append(icon, info);

            const right = document.createElement('div');
            right.className = 'cont-right';
            const status = document.createElement('span');
            status.className = 'badge ' + statusClass;
            status.textContent = item.status || 'unknown';
            const date = document.createElement('span');
            date.className = 'date-chip';
            date.textContent = item.created_at_external
                ? new Date(item.created_at_external).toLocaleDateString()
                : 'No date';
            right.append(status, date);
            card.append(left, right);
            container.append(card);
        });
    }

    // Sync Logic
    document.getElementById('sync-btn').addEventListener('click', async function() {
        const btn = this;
        const btnText = btn.querySelector('.text');
        const btnIcon = btn.querySelector('.icon');

        btn.disabled = true;
        btnText.textContent = 'Syncing...';
        btnIcon.innerHTML = `<svg class="spinner" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="3" stroke-dasharray="30 60"></circle></svg>`;

        try {
            const response = await fetch(`/agents/projects/${PROJECT_ID}/contributions/sync/github`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Entity-ID': CURRENT_ENTITY_ID || ''
                }
            });
            const data = await response.json();

            if (response.ok) {
                showToast(`Synced ${data.synced} contributions`, 'success');
                await fetchContributions();
            } else {
                throw new Error(data.detail || 'Sync failed');
            }
        } catch (error) {
            showToast(error.message, 'error');
        } finally {
            btn.disabled = false;
            btnText.textContent = 'Sync Now';
            btnIcon.innerHTML = '⇄';
        }
    });

    // Filter Logic
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentFilter = btn.dataset.filter;
            renderContributions();
        });
    });

    // Init
    fetchContributions();
    // Refresh interval for timeAgo strings
    setInterval(updateLastSyncTime, 60000);
