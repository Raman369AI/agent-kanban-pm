"""Stored execution identity and recovery observations for task sessions."""
import json
from pathlib import Path
import sys

from agent_kanban_pm.runtime.process_launcher import RunnerState
from agent_kanban_pm.runtime.run_guard import execution_state


def launch_spec(session):
    return json.loads(session.launch_spec_json) if session.launch_spec_json else None


def runner_name(session, legacy_name):
    spec = launch_spec(session)
    return spec['runner_name'] if spec else legacy_name


def make_spec(run_token, name, args, stage_id):
    return json.dumps({
        'version': 1,
        'runner_name': name + '-' + run_token,
        'receipt': str(Path.home() / '.kanban' / 'runs' / (run_token + '.json')),
        'args': args,
        'stage_id': stage_id,
    })


def guarded_args(spec):
    return [sys.executable, '-m', 'agent_kanban_pm.runtime.run_guard', spec['receipt'], *spec['args']]


def receipt_state(session):
    spec = launch_spec(session)
    return RunnerState(*execution_state(spec['receipt'])) if spec else None
