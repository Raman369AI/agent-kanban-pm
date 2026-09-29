    // --- Git changes in task review ---
    function renderTaskDiffLines(patch) {
        return patch.split('\n').map(function(line) {
            var kind = line.startsWith('+++') || line.startsWith('---') ? 'file' :
                line.startsWith('+') ? 'added' :
                line.startsWith('-') ? 'removed' :
                line.startsWith('@@') ? 'hunk' :
                line.startsWith('diff --git ') ? 'file' : '';
            return '<span class="task-diff-line ' + kind + '">' + escapeHtml(line) + '</span>';
        }).join('');
    }
    function renderTaskDiffFiles(patch) {
        var chunks = patch.split(/(?=^diff --git )/m).filter(Boolean);
        if (!chunks.length) chunks = [patch];
        return {
            count: chunks.length,
            html: chunks.map(function(chunk, index) {
                var match = chunk.match(/^\+\+\+ b\/(.*)$/m) || chunk.match(/^--- a\/(.*)$/m);
                var title = match ? match[1] : 'Patch ' + (index + 1);
                return '<details class="task-diff-file"' + (index === 0 ? ' open' : '') + '>' +
                    '<summary><span>' + escapeHtml(title) + '</span></summary>' +
                    '<pre class="task-diff-patch">' + renderTaskDiffLines(chunk) + '</pre></details>';
            }).join('')
        };
    }
    function renderTaskGitDiff(taskId, data) {
        var el = document.getElementById('task-git-diff-' + taskId);
        var source = document.getElementById('task-git-diff-source-' + taskId);
        if (!el || !source) return;
        var sourceLabel = {worktree: 'Live task worktree', branch: 'Committed task branch', review: 'Saved review snapshot'};
        source.textContent = sourceLabel[data.source] || 'Unavailable';
        if (data.branch) source.textContent += ' · ' + data.branch;
        if (!data.diff) {
            el.innerHTML = '<p class="text-secondary task-diff-empty">' +
                escapeHtml(data.message || 'No changes from the task base.') + '</p>';
            return;
        }
        var rendered = renderTaskDiffFiles(data.diff);
        el.innerHTML = (data.message ? '<p class="text-secondary task-diff-note">' + escapeHtml(data.message) + '</p>' : '') +
            '<div class="task-diff-meta">' + rendered.count + ' file' + (rendered.count === 1 ? '' : 's') +
            (data.base_ref ? ' · compared with ' + escapeHtml(data.base_ref) : '') +
            (data.truncated ? ' · preview truncated' : '') + '</div>' + rendered.html;
    }
    async function fetchTaskGitDiff(taskId) {
        var source = document.getElementById('task-git-diff-source-' + taskId);
        if (source) source.textContent = 'Loading…';
        try {
            var resp = await fetch('/agents/projects/' + PROJECT_ID + '/tasks/' + taskId + '/git-diff');
            if (!resp.ok) throw new Error('Could not load Git diff (' + resp.status + ')');
            renderTaskGitDiff(taskId, await resp.json());
        } catch(e) {
            renderTaskGitDiff(taskId, {source: 'none', diff: '', message: e.message});
        }
    }
    function refreshTaskGitDiff(taskId) {
        fetchTaskGitDiff(taskId);
    }

    // --- Review decisions and their immutable patch snapshots ---
    var taskReviewRecords = {};
    function showTaskReviewSnapshot(reviewId, details) {
        if (!details.open) return;
        var body = details.querySelector('.task-review-snapshot-body');
        var review = taskReviewRecords[reviewId];
        if (!body || !review || body.dataset.loaded) return;
        if (review.diff_content) {
            var rendered = renderTaskDiffFiles(review.diff_content);
            body.innerHTML = '<div class="task-diff-meta">' + rendered.count + ' file' +
                (rendered.count === 1 ? '' : 's') + ' in this review snapshot</div>' + rendered.html;
        } else {
            body.innerHTML = '<p class="text-secondary">This review has no saved patch.</p>';
        }
        body.dataset.loaded = 'true';
    }
    async function decideTaskReview(taskId, reviewId, decision) {
        var review = taskReviewRecords[reviewId];
        if (!review || review.task_id !== taskId || review.status !== 'pending') return;
        if (decision !== 'approved' && decision !== 'rejected') return;
        var card = document.getElementById('task-review-' + reviewId);
        var note = card && card.querySelector('.task-review-note');
        var buttons = card ? card.querySelectorAll('.task-review-actions button') : [];
        buttons.forEach(function(button) { button.disabled = true; });
        try {
            await apiFetch('/agents/diff-reviews/' + reviewId, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json', 'X-Entity-ID': CURRENT_ENTITY_ID},
                body: JSON.stringify({status: decision, review_notes: note ? note.value.trim() : ''})
            }, 'Could not save review decision');
            showToast(decision === 'approved' ? 'Review approved. Git is unchanged.' : 'Review rejected. Git is unchanged.', 'success');
            await fetchTaskReviews(taskId);
        } catch (error) {
            buttons.forEach(function(button) { button.disabled = false; });
            showToast(error.message, 'error');
        }
    }
    var reviewFetchVersions = {};
    async function fetchTaskReviews(taskId) {
        var requestVersion = (reviewFetchVersions[taskId] || 0) + 1;
        reviewFetchVersions[taskId] = requestVersion;
        var el = document.getElementById('task-reviews-list-' + taskId);
        if (!el) return;
        try {
            var reviews = await apiFetch(
                '/agents/projects/' + PROJECT_ID + '/diff-reviews?task_id=' + taskId + '&limit=20',
                {}, 'Could not load reviews'
            );
            if (reviewFetchVersions[taskId] !== requestVersion) return;
            var drafts = {};
            el.querySelectorAll('.task-review-card').forEach(function(card) {
                var note = card.querySelector('.task-review-note');
                if (note) drafts[card.id] = note.value;
            });
            var taskReviews = reviews.filter(function(r) { return r.task_id === taskId; });
            if (!taskReviews.length) {
                el.innerHTML = '<p class="text-secondary" style="font-size:0.8rem;">No reviews for this task yet.</p>';
                return;
            }
            var statusColors = {pending:'#fbbf24', approved:'#34d399', rejected:'#f87171', changes_requested:'#60a5fa'};
            el.innerHTML = taskReviews.map(function(r) {
                var reviewId = Number(r.id);
                taskReviewRecords[reviewId] = r;
                var color = statusColors[r.status] || '#9ca3af';
                var actions = r.status === 'pending'
                    ? '<textarea class="task-review-note" aria-label="Review note for review ' + reviewId + '" placeholder="Optional review note">' + escapeHtml(drafts['task-review-' + reviewId] || '') + '</textarea>' +
                      '<div class="task-review-actions">' +
                      '<button type="button" class="btn btn-sm btn-primary" onclick="decideTaskReview(' + taskId + ',' + reviewId + ',\'approved\')">Approve</button>' +
                      '<button type="button" class="btn btn-sm task-review-reject" onclick="decideTaskReview(' + taskId + ',' + reviewId + ',\'rejected\')">Reject</button></div>'
                    : (r.review_notes ? '<p class="task-review-existing-note">' + escapeHtml(r.review_notes) + '</p>' : '');
                return '<div class="insight-item task-review-card" id="task-review-' + reviewId + '">' +
                    '<div class="task-review-card-title"><strong>Review #' + reviewId + '</strong>' +
                    '<span class="task-review-status" style="--review-status-color:' + color + '">' + escapeHtml(r.status.replace(/_/g, ' ')) + '</span></div>' +
                    (r.summary ? '<p class="task-review-summary">' + escapeHtml(r.summary) + '</p>' : '') +
                    '<details class="task-review-snapshot" ontoggle="showTaskReviewSnapshot(' + reviewId + ',this)">' +
                    '<summary>View this review’s saved patch</summary><div class="task-review-snapshot-body"></div></details>' +
                    actions +
                    '<div class="insight-meta">' + timeAgo(r.created_at) + '</div></div>';
            }).join('');
        } catch(error) {
            el.innerHTML = '<p class="text-danger">' + escapeHtml(error.message) + '</p>';
        }
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
