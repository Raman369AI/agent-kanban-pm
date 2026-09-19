// Main UI JavaScript
window.escapeHtml = function(value) {
    return String(value === null || value === undefined ? '' : value).replace(
        /[&<>"']/g,
        function(character) {
            return {
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#39;'
            }[character];
        }
    );
};

// Shared response handling keeps server validation/authorization messages visible.
window.apiErrorMessage = function(data, fallback) {
    if (data && typeof data.detail === 'string') return data.detail;
    if (data && Array.isArray(data.detail)) {
        return data.detail.map(function(item) {
            return item.msg || String(item);
        }).join('; ');
    }
    if (data && typeof data.error === 'string') return data.error;
    if (typeof data === 'string' && data.trim()) return data.trim();
    return fallback || 'Request failed';
};

window.apiFetch = async function(input, init, fallback) {
    var response = await window.fetch(input, init);
    var text = await response.text();
    var data = null;
    if (text) {
        try {
            data = JSON.parse(text);
        } catch (error) {
            data = text;
        }
    }
    if (!response.ok) {
        throw new Error(window.apiErrorMessage(data, fallback || response.statusText));
    }
    return data;
};

window.confirmAction = function(options) {
    options = options || {};
    return new Promise(function(resolve) {
        var previous = document.activeElement;
        var overlay = document.createElement('div');
        overlay.className = 'app-confirm-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');

        var card = document.createElement('div');
        card.className = 'app-confirm-card';
        var title = document.createElement('h2');
        title.id = 'app-confirm-title-' + Date.now();
        title.textContent = options.title || 'Confirm action';
        overlay.setAttribute('aria-labelledby', title.id);
        var message = document.createElement('p');
        message.textContent = options.message || 'Are you sure?';
        var actions = document.createElement('div');
        actions.className = 'app-confirm-actions';
        var cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn';
        cancel.textContent = options.cancelLabel || 'Cancel';
        var confirm = document.createElement('button');
        confirm.type = 'button';
        confirm.className = options.danger ? 'btn btn-danger' : 'btn btn-primary';
        confirm.textContent = options.confirmLabel || 'Confirm';
        actions.append(cancel, confirm);
        card.append(title, message, actions);
        overlay.append(card);
        document.body.append(overlay);

        function finish(value) {
            document.removeEventListener('keydown', onKey, true);
            overlay.remove();
            if (previous && previous.isConnected) previous.focus();
            resolve(value);
        }
        function onKey(event) {
            if (event.key === 'Escape') {
                event.preventDefault();
                finish(false);
            }
        }
        cancel.addEventListener('click', function() { finish(false); });
        confirm.addEventListener('click', function() { finish(true); });
        overlay.addEventListener('click', function(event) {
            if (event.target === overlay) finish(false);
        });
        document.addEventListener('keydown', onKey, true);
        confirm.focus();
    });
};

var modalReturnFocus = {};
function modalFocusable(modal) {
    return Array.from(modal.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), ' +
        'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(function(el) { return el.offsetParent !== null; });
}

window.openModal = function(id) {
    var modal = document.getElementById(id);
    if (!modal) return;
    if (modal.style.display === 'none' || !modal.style.display) {
        modalReturnFocus[id] = document.activeElement;
    }
    modal.style.display = 'flex';
    modal.setAttribute('aria-hidden', 'false');
    window.setTimeout(function() {
        var focusable = modalFocusable(modal);
        var preferred = modal.querySelector('[autofocus]');
        if (preferred && preferred.offsetParent !== null) preferred.focus();
        else if (focusable.length) focusable[0].focus();
        else {
            modal.setAttribute('tabindex', '-1');
            modal.focus();
        }
    }, 0);
};

window.closeModal = function(id) {
    var modal = document.getElementById(id);
    if (!modal) return;
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    var returnTo = modalReturnFocus[id];
    delete modalReturnFocus[id];
    if (returnTo && document.contains(returnTo) && typeof returnTo.focus === 'function') {
        returnTo.focus();
    }
};

document.addEventListener('keydown', function(event) {
    var visible = Array.from(document.querySelectorAll('.modal-overlay, .project-modal-overlay, .folder-picker-overlay'))
        .filter(function(modal) {
            return modal.getAttribute('aria-hidden') !== 'true' &&
                modal.style.display !== 'none' && window.getComputedStyle(modal).display !== 'none';
        });
    var modal = visible[visible.length - 1];
    if (!modal) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        var cancelHandler = modal.dataset.cancelHandler;
        if (cancelHandler && typeof window[cancelHandler] === 'function') {
            window[cancelHandler]();
        } else {
            window.closeModal(modal.id);
        }
        return;
    }
    if (event.key !== 'Tab') return;
    var focusable = modalFocusable(modal);
    if (!focusable.length) {
        event.preventDefault();
        modal.focus();
        return;
    }
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
});

document.addEventListener('DOMContentLoaded', function() {
    'use strict';

    // White and Night are the only selectable themes. Older saved Blue and
    // Rose values are normalized by theme.js before this script runs.
    const themeButtons = document.querySelectorAll('[data-set-theme]');
    function setTheme(theme) {
        if (theme !== 'light' && theme !== 'dark') return;
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
        themeButtons.forEach(function(button) {
            button.setAttribute('aria-pressed', String(button.dataset.setTheme === theme));
        });
    }
    themeButtons.forEach(function(button) {
        button.addEventListener('click', function() { setTheme(button.dataset.setTheme); });
    });
    setTheme(document.documentElement.getAttribute('data-theme') || 'light');

    // View toggle
    const viewToggle = document.getElementById('view-toggle');
    
    if (viewToggle) {
        viewToggle.hidden = !document.getElementById('board-main-revamp');
        viewToggle.setAttribute('aria-label', 'Switch board and list view');
        viewToggle.addEventListener('click', function() {
            const currentView = document.documentElement.getAttribute('data-view') || 'grid';
            const newView = currentView === 'grid' ? 'list' : 'grid';
            
            document.documentElement.setAttribute('data-view', newView);
            localStorage.setItem('view', newView);
            viewToggle.textContent = newView === 'grid' ? 'Switch to list' : 'Switch to board';
            viewToggle.setAttribute('aria-pressed', String(newView === 'list'));
            
            // Trigger view change event
            window.dispatchEvent(new CustomEvent('viewchange', { detail: { view: newView } }));
        });

        // Set initial icon
        const savedView = localStorage.getItem('view') || 'grid';
        viewToggle.textContent = savedView === 'grid' ? 'Switch to list' : 'Switch to board';
        viewToggle.setAttribute('aria-pressed', String(savedView === 'list'));
    }

    // Density selector
    const densitySelect = document.getElementById('density-select');
    
    if (densitySelect) {
        const savedDensity = localStorage.getItem('density') || 'comfortable';
        densitySelect.value = savedDensity;
        
        densitySelect.addEventListener('change', function() {
            const density = this.value;
            document.documentElement.setAttribute('data-density', density);
            localStorage.setItem('density', density);
        });
    }

    // Sidebar toggle
    const sidebarToggle = document.getElementById('sidebar-toggle');
    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', function() {
            const currentSidebar = document.documentElement.getAttribute('data-sidebar') || 'expanded';
            const newSidebar = currentSidebar === 'expanded' ? 'collapsed' : 'expanded';
            
            document.documentElement.setAttribute('data-sidebar', newSidebar);
            localStorage.setItem('sidebar', newSidebar);
        });

        // Set initial state
        const savedSidebar = localStorage.getItem('sidebar') || 'expanded';
        document.documentElement.setAttribute('data-sidebar', savedSidebar);
    }

    // Mobile navigation drawer: the sidebar is hidden below 992px, so the
    // header button is the only way to reach global navigation there.
    const mobileNavToggle = document.getElementById('mobile-nav-toggle');
    const sidebarBackdrop = document.getElementById('sidebar-backdrop');
    function closeMobileNav(returnFocus) {
        document.documentElement.setAttribute('data-sidebar-open', 'false');
        if (mobileNavToggle) mobileNavToggle.setAttribute('aria-expanded', 'false');
        if (sidebarBackdrop) sidebarBackdrop.hidden = true;
        if (returnFocus && mobileNavToggle) mobileNavToggle.focus();
    }
    if (mobileNavToggle) {
        mobileNavToggle.addEventListener('click', function() {
            if (document.documentElement.getAttribute('data-sidebar-open') === 'true') {
                closeMobileNav(false);
                return;
            }
            document.documentElement.setAttribute('data-sidebar-open', 'true');
            mobileNavToggle.setAttribute('aria-expanded', 'true');
            if (sidebarBackdrop) sidebarBackdrop.hidden = false;
            const firstLink = document.querySelector('.sidebar-nav a');
            if (firstLink) firstLink.focus({ preventScroll: true });
        });
        document.querySelectorAll('.sidebar-nav a').forEach(function(link) {
            link.addEventListener('click', () => closeMobileNav(false));
        });
        if (sidebarBackdrop) {
            sidebarBackdrop.addEventListener('click', () => closeMobileNav(false));
        }
        window.addEventListener('resize', function() {
            if (window.innerWidth > 992 && document.documentElement.getAttribute('data-sidebar-open') === 'true') {
                closeMobileNav(false);
            }
        });
        document.addEventListener('keydown', function(event) {
            if (event.key !== 'Escape') return;
            if (document.documentElement.getAttribute('data-sidebar-open') === 'true') {
                closeMobileNav(true);
            }
        });
    }

    // Auto-refresh data every 30 seconds
    if (typeof window.refreshData === 'function') {
        setInterval(window.refreshData, 30000);
    }

    // Smooth transitions on page load
    document.body.style.opacity = '0';
    document.body.style.transition = 'opacity 0.3s ease';
    requestAnimationFrame(() => {
        document.body.style.opacity = '1';
    });
});

// Utility function to format dates
function formatDate(dateString) {
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);
    
    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins} minute${diffMins > 1 ? 's' : ''} ago`;
    if (diffHours < 24) return `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
    if (diffDays < 7) return `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;
    
    return date.toLocaleDateString();
}

// Export for use in other scripts
window.formatDate = formatDate;
