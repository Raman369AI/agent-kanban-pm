(function (root) {
    'use strict';

    function readableText(value) {
        return String(value || '')
            .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, '')
            .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
            .replace(/\r(?!\n)/g, '\n')
            .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');
    }

    function usefulLine(value) {
        var line = value.replace(/^\s*[│┃]\s?/, '').replace(/\s*[│┃]\s*$/, '').trim();
        if (!line || /^[─━═┄┈╭╮╰╯┌┐└┘┬┴┼┤├│┃▀▄░▒▓\s]+$/.test(line)) return '';
        if (/^(?:\? for shortcuts|type your message|press esc to|ctrl\+\w+ to)/i.test(line)) return '';
        return line.length > 240 ? line.slice(0, 237) + '…' : line;
    }

    function focused(activities, lineLimit) {
        var output = [];
        var events = [];
        (activities || []).forEach(function (entry) {
            if (entry.source === 'tmux_pane') {
                readableText(entry.message).split('\n').forEach(function (rawLine) {
                    var line = usefulLine(rawLine);
                    if (line) output.push(line);
                });
            } else {
                var message = readableText(entry.message).replace(/\s+/g, ' ').trim();
                if (message.length > 240) message = message.slice(0, 237) + '…';
                if (message) events.push((entry.activity_type || 'event') + ': ' + message);
            }
        });

        var seen = new Set();
        var recent = [];
        var maxLines = lineLimit || 18;
        for (var i = output.length - 1; i >= 0 && recent.length < maxLines; i--) {
            var key = output[i].toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            recent.push(output[i]);
        }
        recent.reverse();

        var sections = [];
        if (recent.length) sections.push('Recent output\n' + recent.join('\n'));
        if (events.length) sections.push('Key events\n' + events.slice(-4).join('\n'));
        return sections.join('\n\n') || 'No agent output recorded yet.';
    }

    function raw(activities, session) {
        var lines = [];
        if (session) {
            lines.push('$ ' + (session.command || 'session started'));
            if (session.workspace_path) lines.push('# workspace: ' + session.workspace_path);
            lines.push('');
        }
        (activities || []).forEach(function (entry) {
            if (entry.source === 'tmux_pane') {
                lines.push(readableText(entry.message).trimEnd());
                return;
            }
            var stamp = entry.created_at ? new Date(entry.created_at).toLocaleTimeString() : '';
            lines.push('[' + stamp + '] ' + (entry.activity_type || entry.source || 'event') + ': ' + readableText(entry.message));
            if (entry.command) lines.push('  $ ' + entry.command);
            if (entry.file_path) lines.push('  file: ' + entry.file_path);
            lines.push('');
        });
        return lines.join('\n').trim() || 'No activity recorded for this session.';
    }

    root.KanbanTerminalFeed = {focused: focused, raw: raw};
})(window);
