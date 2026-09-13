/* Role settings own their draft state; saving one row never resets another. */
(function () {
    'use strict';
    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text) node.textContent = text;
        if (className) node.className = className;
        return node;
    }
    function field(row, title, control, id) {
        const label = element('label', title, 'role-field');
        control.id = id;
        control.className = 'form-input';
        label.htmlFor = id;
        label.append(control);
        row.append(label);
        return control;
    }
    function choices(select, options, value) {
        select.replaceChildren();
        options.forEach(([key, label]) => select.add(new Option(label, key)));
        select.value = value;
    }
    window.renderRoleCliPanel = function (data) {
        const list = document.getElementById('team-list');
        list.replaceChildren();
        const assigned = new Map((data.roles || []).map(role => [role.role, role]));
        const names = data.role_names || [...assigned.keys()];
        names.forEach(name => {
            let current = assigned.get(name);
            const candidates = new Map((data.candidates || [])
                .filter(candidate => candidate.installed).map(candidate => [candidate.agent, candidate]));
            if (current && !candidates.has(current.agent)) candidates.set(current.agent, current);
            const form = element('form', '', 'role-settings-row');
            form.append(element('strong', name));
            const agent = field(form, 'Agent', element('select'), 'role-agent-' + name);
            choices(agent, [['', 'Choose an agent'], ...[...candidates.values()].map(c =>
                [c.agent, c.display_name + (c.installed ? '' : ' (unavailable)')])], current?.agent || '');
            const modelSlot = element('div');
            form.append(modelSlot);
            let model;
            function updateModels(value) {
                modelSlot.replaceChildren();
                const candidate = candidates.get(agent.value);
                const models = candidate?.models || [];
                model = field(modelSlot, 'Model', element(models.length ? 'select' : 'input'), 'role-model-' + name);
                if (models.length) {
                    const options = models.map(id => [id, id === 'default' || id === candidate.agent + '-default' ? 'CLI default' : id]);
                    if (value && !models.includes(value)) options.unshift([value, value + ' (not in adapter list)']);
                    choices(model, options, value || models[0]);
                } else {
                    model.placeholder = 'CLI default';
                    model.value = value || '';
                }
            }
            const mode = field(form, 'Session mode', element('select'), 'role-mode-' + name);
            const modes = [...new Set(['headless', 'interactive', current?.mode].filter(Boolean))];
            choices(mode, modes.map(value => [value, value]), current?.mode || 'headless');
            const autonomy = field(form, 'Approvals', element('select'), 'role-autonomy-' + name);
            choices(autonomy, [['supervised', 'Ask for approval'], ['auto', 'Automatic — bypass approval prompts']],
                current?.autonomy || 'supervised');
            const feedback = element('p', '', 'role-feedback');
            feedback.setAttribute('role', 'status');
            const button = element('button', 'Save role', 'btn btn-primary');
            button.type = 'submit';
            form.append(button, feedback);
            function updateAvailability() {
                button.disabled = !candidates.get(agent.value)?.installed;
            }
            agent.addEventListener('change', () => {
                updateModels(agent.value === current?.agent ? current.model : null);
                updateAvailability();
                feedback.textContent = '';
            });
            form.addEventListener('submit', async event => {
                event.preventDefault();
                const candidate = candidates.get(agent.value);
                if (!candidate?.installed || button.disabled) return;
                const controls = [...form.querySelectorAll('input, select, button')];
                controls.forEach(control => { control.disabled = true; });
                feedback.textContent = 'Saving…';
                try {
                    const result = await window.apiFetch('/ui/api/roles/assign', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-Entity-ID': CURRENT_ENTITY_ID || ''},
                        body: JSON.stringify({role: name, agent: agent.value, command: candidate.command,
                            model: model.value || null, mode: mode.value, autonomy: autonomy.value})
                    }, 'Failed to save role');
                    current = result.roles.find(role => role.role === name);
                    feedback.textContent = 'Saved';
                    if (typeof window.refreshBoardFromServer === 'function') window.refreshBoardFromServer();
                    if (typeof window.onRoleSettingsSaved === 'function') window.onRoleSettingsSaved();
                } catch (error) {
                    feedback.textContent = error.message;
                } finally {
                    controls.forEach(control => { control.disabled = false; });
                    updateAvailability();
                }
            });
            updateModels(current?.model);
            updateAvailability();
            list.append(form);
        });
        if (!names.length) list.append(element('p', 'No roles configured.'));
    };
})();
