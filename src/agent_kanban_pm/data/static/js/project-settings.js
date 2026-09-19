function showSettingsToast(message, type) {
    var toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = 'toast toast-' + (type || 'info');
    toast.style.display = 'block';
    setTimeout(function() { toast.style.display = 'none'; }, 4000);
  }

  async function saveProjectSettings() {
    var btn = document.getElementById('save-settings-btn');
    var feedback = document.getElementById('settings-feedback');
    var name = document.getElementById('settings-name').value;
    var desc = document.getElementById('settings-desc').value;
    var path = document.getElementById('settings-path').value;
    btn.disabled = true;
    try {
      await apiFetch('/ui/projects/' + PROJECT_ID + '/edit', {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json', 'x-entity-id': CURRENT_ENTITY_ID},
        body: JSON.stringify({name: name, description: desc, path: path})
      }, 'Failed to save settings');
      if (feedback) {
        feedback.style.display = 'inline';
        setTimeout(function() { feedback.style.display = 'none'; }, 3000);
      }
    } catch (err) {
      showSettingsToast('Error saving settings: ' + err.message, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  async function deleteThisProject() {
    var confirmed = await confirmAction({
      title: 'Delete project?',
      message: 'This permanently deletes the project, its board stages, task history, and sessions.',
      confirmLabel: 'Delete project',
      danger: true
    });
    if (!confirmed) return;
    apiFetch('/ui/projects/' + PROJECT_ID, {
      method: 'DELETE',
      headers: {'x-entity-id': CURRENT_ENTITY_ID}
    }, 'Failed to delete project').then(function() {
      window.location.href = '/ui/projects';
    }).catch(function(err) {
      showSettingsToast('Error deleting project: ' + err.message, 'error');
    });
  }
