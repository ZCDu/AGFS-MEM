"""Console entrypoints for the independent API and compression worker."""

import argparse
import asyncio
from contextlib import suppress
import signal
from typing import Awaitable, Callable, Sequence

import uvicorn

from short_term_memory.config import ShortTermMemorySettings, load_settings
from short_term_memory.service.runtime import ServiceRuntime


def _arguments(argv: Sequence[str] | None, description: str) -> None:
    parser = argparse.ArgumentParser(description=description)
    parser.parse_args(argv)


def api_main(argv: Sequence[str] | None = None) -> None:
    _arguments(argv, "Run the short-term memory HTTP API")
    settings = load_settings()
    uvicorn.run(
        "short_term_memory.service.runtime:create_runtime_app",
        factory=True,
        host=settings.api.host,
        port=settings.api.port,
        workers=settings.api.workers,
        limit_concurrency=max(100, settings.api.concurrency_limit),
    )


async def run_worker_process(
    settings: ShortTermMemorySettings,
    *,
    runtime_start: Callable[
        [ShortTermMemorySettings], Awaitable[ServiceRuntime]
    ] = ServiceRuntime.start,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run bounded worker loops until cancellation or a termination signal."""

    runtime = await runtime_start(settings)
    loop = asyncio.get_running_loop()
    stopping = stop_event or asyncio.Event()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            pass

    worker_task = asyncio.create_task(
        runtime.worker.run_forever(stop_event=stopping)
    )
    signal_task = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait(
            {worker_task, signal_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if worker_task in done:
            await worker_task
        else:
            try:
                async with asyncio.timeout(
                    settings.compression_queue.shutdown_grace_seconds
                ):
                    await asyncio.shield(worker_task)
            except TimeoutError:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
    finally:
        signal_task.cancel()
        if not worker_task.done():
            worker_task.cancel()
        await asyncio.gather(signal_task, worker_task, return_exceptions=True)
        for signum in installed:
            loop.remove_signal_handler(signum)
        await runtime.close()


def worker_main(argv: Sequence[str] | None = None) -> None:
    _arguments(argv, "Run the short-term memory compression worker")
    settings = load_settings()
    asyncio.run(run_worker_process(settings))
