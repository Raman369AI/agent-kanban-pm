var roleEditorLoaded = false;
    var roleEditorDirty = false;
    var roleQuery = new URLSearchParams(window.location.search);
    var requestedRole = roleQuery.get('focus') || '';
    var roleReturnUrl = roleQuery.get('return') || '';
    var returnLink = document.getElementById('role-return-link');
    if (returnLink && roleReturnUrl.indexOf('/ui/projects/') === 0) {
        returnLink.href = roleReturnUrl;
        returnLink.hidden = false;
    }

    function focusRequestedRole() {
        if (!requestedRole) return;
        var row = document.querySelector('.role-settings-row[data-role="' +
            CSS.escape(requestedRole) + '"]');
        if (!row) return;
        row.classList.add('role-settings-focus');
        row.scrollIntoView({block: 'center'});
        var select = row.querySelector('select');
        if (select) select.focus();
    }
    window.onRoleSettingsSaved = function() { roleEditorDirty = true; };

    function toggleRoleEditor() {
        var container = document.getElementById('role-editor-container');
        var btnText = document.getElementById('role-editor-btn-text');
        if (!container) return;

        if (container.style.display === 'none') {
            container.style.display = 'block';
            if (btnText) btnText.textContent = 'Hide editor';
            if (!roleEditorLoaded) {
                roleEditorLoaded = true;
                apiFetch('/ui/api/roles', {}, 'Failed to load roles').then(function(data) {
                    if (window.renderRoleCliPanel) {
                        window.renderRoleCliPanel(data);
                        focusRequestedRole();
                    }
                }).catch(function(err) {
                    var list = document.getElementById('team-list');
                    if (list) {
                        list.replaceChildren();
                        var error = document.createElement('p');
                        error.className = 'text-danger';
                        error.textContent = 'Failed to load roles: ' + err.message;
                        list.append(error);
                    }
                });
            }
        } else {
            if (roleEditorDirty) {
                window.location.reload();
                return;
            }
            container.style.display = 'none';
            if (btnText) btnText.textContent = 'Configure roles';
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        if (requestedRole) toggleRoleEditor();
    });
