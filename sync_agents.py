#!/usr/bin/env python3
"""
Agent Sync — now backed by the Adapter Registry.

Previously: hardcoded list of CLI tools.
Now: scans ~/.kanban/agents/*.yaml for declarative agent specs.

This module is imported by main.py and called during startup.
"""

import logging
from kanban_runtime.adapter_loader import init_adapter_registry

logger = logging.getLogger(__name__)


async def sync_cli_agents():
    """
    Sync DB entities with the adapter registry.
    Drops bundled adapters to ~/.kanban/agents/ on first boot,
    then upserts Entity rows for every YAML found.
    """
    logger.info("Starting adapter registry sync...")
    await init_adapter_registry()
    logger.info("Adapter registry sync complete.")
