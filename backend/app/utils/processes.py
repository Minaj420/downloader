"""Shared subprocess termination for the queue and the job runner."""

import asyncio


async def ensure_process_terminated(
    process: asyncio.subprocess.Process, grace_seconds: float
) -> None:
    """Terminate a process, wait briefly, then force kill on timeout."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
