// Apply saved appearance before the page paints.
(function() {
    'use strict';

    const savedTheme = localStorage.getItem('theme');
    const theme = savedTheme === 'dark' || savedTheme === 'blue' ? 'dark' : 'light';
    if (savedTheme !== theme) localStorage.setItem('theme', theme);
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.setAttribute('data-density', localStorage.getItem('density') || 'comfortable');
    document.documentElement.setAttribute('data-view', localStorage.getItem('view') || 'grid');

    function enableTransitions() {
        requestAnimationFrame(function() {
            document.body.classList.add('theme-transition');
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', enableTransitions);
    } else {
        enableTransitions();
    }
})();
