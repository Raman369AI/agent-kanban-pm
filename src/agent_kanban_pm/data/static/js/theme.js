// Theme and Preferences Management
(function() {
    'use strict';

    // Load saved preferences on page load
    function loadPreferences() {
        const theme = localStorage.getItem('theme') || 'light';
        const density = localStorage.getItem('density') || 'comfortable';
        const view = localStorage.getItem('view') || 'grid';
        
        document.documentElement.setAttribute('data-theme', theme);
        document.documentElement.setAttribute('data-density', density);
        document.documentElement.setAttribute('data-view', view);
        
        // Add transition class after initial paint to prevent flash
        requestAnimationFrame(function() {
            document.body.classList.add('theme-transition');
        });
        
        return { theme, density, view };
    }

    // Initialize preferences on DOM load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadPreferences);
    } else {
        loadPreferences();
    }
})();