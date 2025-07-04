import contextlib
import logging
from datetime import datetime
from typing import AsyncIterator, Callable, Coroutine, Iterator

import anyio

from inspect_ai._util._async import configured_async_backend, run_coroutine
from inspect_ai._util.platform import running_in_notebook
from inspect_ai.log import EvalStats

from ...util import throttle
from ...util._concurrency import concurrency_status_display
from ..core.config import task_config_str
from ..core.display import (
    TR,
    Display,
    Progress,
    TaskCancelled,
    TaskDisplay,
    TaskDisplayMetric,
    TaskError,
    TaskProfile,
    TaskResult,
    TaskScreen,
    TaskSpec,
    TaskSuccess,
    TaskWithResult,
)
from ..core.footer import task_http_retries_str
from ..core.panel import task_title
from ..core.results import task_metric


class LogDisplay(Display):
    def __init__(self) -> None:
        self.total_tasks: int = 0
        self.tasks: list[TaskWithResult] = []
        self.parallel = False

    def print(self, message: str) -> None:
        logging.info(message, stacklevel=2)

    @contextlib.contextmanager
    def progress(self, total: int) -> Iterator[Progress]:
        yield LogProgress(total)

    def run_task_app(self, main: Callable[[], Coroutine[None, None, TR]]) -> TR:
        if running_in_notebook():
            return run_coroutine(main())
        else:
            return anyio.run(main, backend=configured_async_backend())

    @contextlib.contextmanager
    def suspend_task_app(self) -> Iterator[None]:
        yield

    @contextlib.asynccontextmanager
    async def task_screen(
        self, tasks: list[TaskSpec], parallel: bool
    ) -> AsyncIterator[TaskScreen]:
        self.total_tasks = len(tasks)
        self.tasks = []
        self.parallel = parallel
        logging.info(f"Running {self.total_tasks} tasks...", stacklevel=3)
        yield TaskScreen()

    @contextlib.contextmanager
    def task(self, profile: TaskProfile) -> Iterator[TaskDisplay]:
        # Create and yield task display
        task = TaskWithResult(profile, None)
        self.tasks.append(task)
        yield LogTaskDisplay(task)
        self._log_result(task)
        self._log_status()

    def display_counter(self, caption: str, value: str) -> None:
        logging.info(f"{caption}: {value}", stacklevel=2)

    def _log_status(self) -> None:
        """Log status updates for all tasks"""
        completed_tasks = sum(1 for task in self.tasks if task.result is not None)
        total_tasks = len(self.tasks)
        logging.info(f"{completed_tasks}/{total_tasks} tasks complete", stacklevel=4)

    def _task_stats_str(self, stats: EvalStats) -> str:
        # eval time
        started = datetime.fromisoformat(stats.started_at)
        completed = datetime.fromisoformat(stats.completed_at)
        elapsed = completed - started
        res = f"total time: {elapsed}"
        # token usage
        for model, usage in stats.model_usage.items():
            if (
                usage.input_tokens_cache_read is not None
                or usage.input_tokens_cache_write is not None
            ):
                input_tokens_cache_read = usage.input_tokens_cache_read or 0
                input_tokens_cache_write = usage.input_tokens_cache_write or 0
                input_tokens = f"I: {usage.input_tokens:,}, CW: {input_tokens_cache_write:,}, CR: {input_tokens_cache_read:,}"
            else:
                input_tokens = f"I: {usage.input_tokens:,}"

            if usage.reasoning_tokens is not None:
                reasoning_tokens = f", R: {usage.reasoning_tokens:,}"
            else:
                reasoning_tokens = ""

            model_token_usage = f"{model}:  {usage.total_tokens:,} tokens [{input_tokens}, O: {usage.output_tokens:,}{reasoning_tokens}]"

            res += f"\n{model_token_usage}"

        return res

    def _log_result(self, task: TaskWithResult) -> None:
        """Log final result"""
        title = task_title(task.profile, True)
        if isinstance(task.result, TaskCancelled):
            config = task_config_str(task.profile)
            stats = self._task_stats_str(task.result.stats)
            logging.info(
                f"{title} cancelled ({task.result.samples_completed}/{task.profile.samples} logged)\n{config}\n{stats}",
                stacklevel=4,
            )
        elif isinstance(task.result, TaskError):
            logging.error(
                f"{title} failed ({task.result.samples_completed}/{task.profile.samples} logged)",
                exc_info=(
                    task.result.exc_type,
                    task.result.exc_value,
                    task.result.traceback,
                ),
                stacklevel=4,
            )
        elif isinstance(task.result, TaskSuccess):
            config = task_config_str(task.profile)
            stats = self._task_stats_str(task.result.stats)
            logging.info(f"{title} succeeded\n{config}\n{stats}", stacklevel=4)


class LogProgress(Progress):
    def __init__(self, total: int):
        self.total = total
        self.current = 0

    def update(self, n: int = 1) -> None:
        self.current += n

    def complete(self) -> None:
        self.current = self.total


class LogTaskDisplay(TaskDisplay):
    def __init__(self, task: TaskWithResult):
        self.task = task
        self.progress_display: LogProgress | None = None
        self.samples_complete = 0
        self.samples_total = 0
        self.current_metrics: list[TaskDisplayMetric] | None = None

    @contextlib.contextmanager
    def progress(self) -> Iterator[Progress]:
        self.progress_display = LogProgress(self.task.profile.steps)
        yield self.progress_display

    @throttle(5)
    def _log_status_throttled(self, stacklevel: int) -> None:
        self._log_status(stacklevel=stacklevel + 2)

    def _log_status(self, stacklevel: int) -> None:
        """Log status updates"""
        status_parts: list[str] = []

        # Add task name and model
        status_parts.append(f"Task: {self.task.profile.name}")
        status_parts.append(f"Model: {self.task.profile.model}")

        # Add step progress
        if self.progress_display:
            progress_percent = int(
                self.progress_display.current / self.progress_display.total * 100
            )
            status_parts.append(
                f"Steps: {self.progress_display.current}/{self.progress_display.total} {progress_percent}%"
            )

        # Add sample progress
        status_parts.append(f"Samples: {self.samples_complete}/{self.samples_total}")

        # Add metrics
        if self.current_metrics:
            metric_str = task_metric(self.current_metrics)
            status_parts.append(metric_str)

        # Add resource usage
        resources_dict: dict[str, str] = {}
        for model, resource in concurrency_status_display().items():
            resources_dict[model] = f"{resource[0]}/{resource[1]}"
        resources = ", ".join(
            [f"{key}: {value}" for key, value in resources_dict.items()]
        )
        status_parts.append(resources)

        # Add rate limits
        rate_limits = task_http_retries_str()
        if rate_limits:
            status_parts.append(rate_limits)

        # Print on new line
        logging.info(", ".join(status_parts), stacklevel=stacklevel)

    def sample_complete(self, complete: int, total: int) -> None:
        self.samples_complete = complete
        self.samples_total = total
        self._log_status_throttled(stacklevel=3)

    def update_metrics(self, metrics: list[TaskDisplayMetric]) -> None:
        self.current_metrics = metrics
        self._log_status_throttled(stacklevel=3)

    def complete(self, result: TaskResult) -> None:
        self.task.result = result
        self._log_status(stacklevel=3)
