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
