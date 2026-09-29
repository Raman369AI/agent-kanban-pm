    var expandedCards = {};
    var expandedCardTabs = {};
    var draggedTask = null;
    var expectedLocalMoves = {};


    // --- Notification settings ---
    function initNotificationSettings() {
        var enabled = localStorage.getItem('kanban.notifications.enabled') === 'true';
        var sound = localStorage.getItem('kanban.notifications.sound') === 'true';
        var cb = document.getElementById('notifications-enabled');
        var scb = document.getElementById('notifications-sound');
        if (cb) cb.checked = enabled;
        if (scb) scb.checked = sound;
        if (enabled && 'Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
    }
    function toggleNotifications(enabled) {
        localStorage.setItem('kanban.notifications.enabled', enabled);
        if (enabled && 'Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
    }
    function toggleNotificationSound(enabled) {
        localStorage.setItem('kanban.notifications.sound', enabled);
    }
    function sendOSNotification(title, body, taskId) {
        if (localStorage.getItem('kanban.notifications.enabled') !== 'true') return;
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        var n = new Notification(title, { body: body, tag: 'kanban-approval-' + taskId });
        if (taskId) {
            n.onclick = function() {
                window.focus();
                expandCardAndShowApprovals(taskId);
                n.close();
            };
        }
    }
    function toggleNotificationSettings() {
        var panel = document.getElementById('notification-settings-panel');
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    }

    // --- Bell dropdown ---
    function toggleBellDropdown() {
        var dd = document.getElementById('bell-dropdown');
        dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
    }
    document.addEventListener('click', function(e) {
        var bell = document.getElementById('header-bell-btn');
        var dd = document.getElementById('bell-dropdown');
        if (bell && dd && !bell.contains(e.target) && !dd.contains(e.target)) {
            dd.style.display = 'none';
        }
        var nsp = document.getElementById('notification-settings-panel');
        var nsb = document.querySelector('.notification-settings-btn');
        if (nsb && nsp && !nsb.contains(e.target) && !nsp.contains(e.target)) {
            nsp.style.display = 'none';
        }
    });
