"""Built-in boot-md hook — run ~/.hermes/BOOT.md on gateway startup.

This hook is always registered. It silently skips if no BOOT.md exists.
To activate, create ``~/.hermes/BOOT.md`` with instructions for the
agent to execute on every gateway restart.

Example BOOT.md::

    # Startup Checklist

    1. Check if any cron jobs failed overnight
    2. Send a status update to Discord #general
    3. If there are errors in /opt/app/deploy.log, summarize them

The agent runs in a background thread so it doesn't block gateway
startup. If nothing needs attention, it replies with [SILENT] to
suppress delivery.
"""

import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("hooks.boot-md")

from hermes_constants import get_hermes_home
HERMES_HOME = get_hermes_home()
BOOT_FILE = HERMES_HOME / "BOOT.md"
REALTIME_LOG = HERMES_HOME / "spy" / "spy_realtime.log"   # 实时录制（30分钟滚动）
OBSERVATION_LOG = HERMES_HOME / "spy" / "observation.log"  # 想法分析（累积）
MEMORY_FILE = HERMES_HOME / "MEMORY.md"


def _inject_spy_context_to_memory() -> str:
    """Read spy_realtime.log and synchronously append recent entries to MEMORY.md.
    
    This runs in the main gateway thread before the boot agent starts,
    ensuring session recovery context is always available.
    spy_realtime.log 包含最近 30 分钟的完整对话（Ken + Hermes），
    Hermes 重启后第一时间注入，让 Hermes 知道重启前发生了什么。
    """
    if not REALTIME_LOG.exists():
        return ""
    try:
        content = REALTIME_LOG.read_text(encoding="utf-8").strip()
        if not content:
            return ""
        lines = content.splitlines()
        recent_lines = lines[-50:]  # 保留最近 50 条（约 30 分钟）
        recent = "\n".join(recent_lines)
        
        # Append to MEMORY.md synchronously (main thread, before boot agent)
        try:
            current_memory = MEMORY_FILE.read_text(encoding="utf-8") if MEMORY_FILE.exists() else ""
        except Exception:
            current_memory = ""
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        spy_block = f"\n\n## Spy Recovery Log ({timestamp})\n\n{recent}\n"
        
        # Avoid duplicating the last entry
        if recent.strip() in current_memory:
            logger.info("Spy realtime context already in MEMORY.md, skipping duplicate")
            return recent
        
        updated_memory = current_memory.rstrip() + spy_block
        MEMORY_FILE.write_text(updated_memory, encoding="utf-8")
        logger.info("Spy realtime context appended to MEMORY.md (%d lines)", len(recent_lines))
        
        return recent
    except Exception as e:
        logger.warning("Failed to inject spy context to MEMORY: %s", e)
        return ""


def _build_boot_prompt(content: str, spy_context: str = "") -> str:
    """Wrap BOOT.md content in a system-level instruction."""
    spy_section = ""
    if spy_context:
        spy_section = (
            f"\n## RECENT Spy Realtime Log (last 30 min, append to MEMORY.md)\n\n"
            f"{spy_context}\n\n"
            "ACTION REQUIRED: Append the above Spy realtime log entries to the end of "
            "MEMORY.md (~/.hermes/MEMORY.md) using the write_file tool. "
            "Format: add a new section '## Spy Recovery Log' with these entries.\n\n"
        )
    return (
        "You are running a startup boot checklist. Follow the BOOT.md "
        "instructions below exactly.\n\n"
        "---\n"
        f"{content}\n"
        "---\n\n"
        f"{spy_section}"
        "If nothing needs attention and there is nothing to report, "
        "reply with ONLY: [SILENT]"
    )


def _run_boot_agent(content: str) -> None:
    """Spawn a one-shot agent session to execute the boot instructions."""
    try:
        from run_agent import AIAgent

        # Pre-read spy observation log and inject into boot prompt
        spy_context = _inject_spy_context_to_memory()
        prompt = _build_boot_prompt(content, spy_context=spy_context)
        agent = AIAgent(
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=20,
        )
        result = agent.run_conversation(prompt)
        response = result.get("final_response", "")
        if response and "[SILENT]" not in response:
            logger.info("boot-md completed: %s", response[:200])
        else:
            logger.info("boot-md completed (nothing to report)")
    except Exception as e:
        logger.error("boot-md agent failed: %s", e)


async def handle(event_type: str, context: dict) -> None:
    """Gateway startup handler — run BOOT.md if it exists."""
    if not BOOT_FILE.exists():
        return

    content = BOOT_FILE.read_text(encoding="utf-8").strip()
    if not content:
        return

    logger.info("Running BOOT.md (%d chars)", len(content))

    # Inject spy context synchronously in main thread BEFORE boot agent starts.
    # This ensures session recovery context is written to MEMORY.md regardless
    # of whether the boot agent succeeds or fails.
    _inject_spy_context_to_memory()

    # Run in a background thread so we don't block gateway startup.
    thread = threading.Thread(
        target=_run_boot_agent,
        args=(content,),
        name="boot-md",
        daemon=True,
    )
    thread.start()
