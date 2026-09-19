/* Shared local workspace folder picker. */
(function () {
    'use strict';

    let target = null;
    let current = '';
    let parent = null;
    let home = '';
    let requestSequence = 0;

    function listMessage(message, className) {
        const list = document.getElementById('folder-picker-list');
        if (!list) return;
        list.replaceChildren();
        const item = document.createElement('p');
        item.className = className || 'folder-empty';
        item.textContent = message;
        list.append(item);
    }

    function renderFolders(folders) {
        const list = document.getElementById('folder-picker-list');
        if (!list) return;
        list.replaceChildren();
        if (!folders || !folders.length) {
            listMessage('No subdirectories found.');
            return;
        }
        folders.forEach(folder => {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'folder-row';

            const icon = document.createElement('span');
            icon.className = 'folder-icon';
            icon.textContent = '📁';

            const details = document.createElement('span');
            const name = document.createElement('span');
            name.className = 'folder-name';
            name.textContent = folder.name;
            const folderPath = document.createElement('span');
            folderPath.className = 'folder-path';
            folderPath.textContent = folder.path;
            details.append(name, folderPath);

            const open = document.createElement('span');
            open.className = 'folder-open';
            open.textContent = '›';

            row.append(icon, details, open);
            row.addEventListener('click', () => window.loadFolder(folder.path));
            list.append(row);
        });
    }

    window.openFolderPicker = function (targetInputId) {
        target = document.getElementById(targetInputId);
        if (!target) return;
        window.openModal('folder-picker-modal');
        window.loadFolder(target.value.trim());
    };

    window.closeFolderPicker = function () {
        requestSequence += 1;
        window.closeModal('folder-picker-modal');
    };

    window.loadFolderFromInput = function () {
        const input = document.getElementById('folder-picker-current');
        if (input) window.loadFolder(input.value.trim());
    };

    window.folderPickerUp = function () {
        if (parent) window.loadFolder(parent);
    };

    window.folderPickerGoHome = function () {
        if (home) window.loadFolder(home);
    };

    window.loadFolder = function (path) {
        const input = document.getElementById('folder-picker-current');
        if (!input) return Promise.resolve();
        const requestId = ++requestSequence;
        input.onfocus = function () { requestSequence += 1; };
        const inputAtRequest = input.value;
        const query = path ? '?path=' + encodeURIComponent(path) : '';
        listMessage('Loading...');

        return window.apiFetch('/ui/api/folders' + query, {}, 'Failed to load folders')
            .then(data => {
                if (requestId !== requestSequence || input.value !== inputAtRequest) return;
                current = data.path;
                parent = data.parent;
                home = data.home;
                input.value = data.path;
                renderFolders(data.folders);
            })
            .catch(error => {
                if (requestId !== requestSequence) return;
                listMessage('Error: ' + error.message, 'folder-empty text-danger');
            });
    };

    window.selectCurrentFolder = function () {
        if (target && current) {
            target.value = current;
            target.dispatchEvent(new Event('change', {bubbles: true}));
        }
        window.closeFolderPicker();
    };
})();
