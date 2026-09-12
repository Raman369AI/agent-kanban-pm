// Main UI JavaScript
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
    var visible = Array.from(document.querySelectorAll('.modal-overlay, .project-modal-overlay'))
        .filter(function(modal) {
            return modal.style.display !== 'none' && window.getComputedStyle(modal).display !== 'none';
        });
    var modal = visible[visible.length - 1];
    if (!modal) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        window.closeModal(modal.id);
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

    // Theme cycle: light → dark → blue → rose → light
    const themeCycle = ['light', 'dark', 'blue', 'rose'];
    const themeIcons = { light: '🌙', dark: '☀️', blue: '🌊', rose: '🌹' };

    const themeToggle = document.getElementById('theme-toggle');
    const themeIcon = themeToggle?.querySelector('.icon');
    
    if (themeToggle) {
        themeToggle.addEventListener('click', function() {
            const currentTheme = document.documentElement.getAttribute('data-theme');
            const currentIndex = themeCycle.indexOf(currentTheme);
            const nextIndex = (currentIndex + 1) % themeCycle.length;
            const newTheme = themeCycle[nextIndex];
            
            document.documentElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            if (themeIcon) themeIcon.textContent = themeIcons[newTheme] || '🌙';
        });

        // Set initial icon
        const savedTheme = localStorage.getItem('theme') || 'light';
        if (themeIcon) themeIcon.textContent = themeIcons[savedTheme] || '🌙';
    }

    // View toggle
    const viewToggle = document.getElementById('view-toggle');
    const viewIcon = viewToggle?.querySelector('.icon');
    
    if (viewToggle) {
        viewToggle.hidden = !document.getElementById('board-main-revamp');
        viewToggle.setAttribute('aria-label', 'Switch board layout');
        viewToggle.addEventListener('click', function() {
            const currentView = document.documentElement.getAttribute('data-view') || 'grid';
            const newView = currentView === 'grid' ? 'list' : 'grid';
            
            document.documentElement.setAttribute('data-view', newView);
            localStorage.setItem('view', newView);
            viewIcon.textContent = newView === 'grid' ? '📊' : '📋';
            viewToggle.setAttribute('aria-pressed', String(newView === 'list'));
            
            // Trigger view change event
            window.dispatchEvent(new CustomEvent('viewchange', { detail: { view: newView } }));
        });

        // Set initial icon
        const savedView = localStorage.getItem('view') || 'grid';
        viewIcon.textContent = savedView === 'grid' ? '📊' : '📋';
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
        const savedSidebar = localStorage.getItem('sidebar') || 'collapsed';
        document.documentElement.setAttribute('data-sidebar', savedSidebar);
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
