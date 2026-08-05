from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.models import NodeStatus, NodeTrace, ProjectPatchProposal, RunRecord, RunStatus


class RunStore:
    """进程内运行记录；重启服务后清空，不承担持久化职责。"""

    def __init__(self, max_runs: int = 100) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._node_started: dict[tuple[str, int], float] = {}
        self._lock = asyncio.Lock()
        self._max_runs = max_runs

    async def create(self, workflow_id: str) -> RunRecord:
        async with self._lock:
            if len(self._runs) >= self._max_runs:
                oldest = min(self._runs.values(), key=lambda item: item.created_at)
                self._runs.pop(oldest.run_id, None)
            record = RunRecord(run_id=uuid4().hex, workflow_id=workflow_id)
            self._runs[record.run_id] = record
            return record.model_copy(deep=True)

    async def get(self, run_id: str) -> RunRecord | None:
        async with self._lock:
            record = self._runs.get(run_id)
            return record.model_copy(deep=True) if record else None

    async def set_running(self, run_id: str) -> None:
        async with self._lock:
            record = self._runs[run_id]
            record.status = RunStatus.running

    async def begin_node(
        self,
        run_id: str,
        legacy_node_id: str,
        internal_name: str,
        *,
        system_prompt: str = "",
        user_prompt: str = "",
    ) -> int:
        async with self._lock:
            record = self._runs[run_id]
            trace = NodeTrace(
                legacy_node_id=legacy_node_id,
                internal_name=internal_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            record.traces.append(trace)
            index = len(record.traces) - 1
            record.current_node = legacy_node_id
            self._node_started[(run_id, index)] = perf_counter()
            return index

    async def complete_node(self, run_id: str, index: int, output: Any) -> None:
        async with self._lock:
            trace = self._runs[run_id].traces[index]
            trace.status = NodeStatus.succeeded
            trace.output = output
            trace.finished_at = datetime.now(UTC)
            started = self._node_started.pop((run_id, index), None)
            if started is not None:
                trace.duration_ms = round((perf_counter() - started) * 1000)

    async def fail_node(self, run_id: str, index: int, error: str) -> None:
        async with self._lock:
            trace = self._runs[run_id].traces[index]
            trace.status = NodeStatus.failed
            trace.error = error
            trace.finished_at = datetime.now(UTC)
            started = self._node_started.pop((run_id, index), None)
            if started is not None:
                trace.duration_ms = round((perf_counter() - started) * 1000)

    async def succeed(
        self,
        run_id: str,
        markdown: str,
        proposed_project_patch: ProjectPatchProposal | None = None,
    ) -> None:
        async with self._lock:
            record = self._runs[run_id]
            record.status = RunStatus.succeeded
            record.current_node = None
            record.final_markdown = markdown
            record.proposed_project_patch = proposed_project_patch
            record.finished_at = datetime.now(UTC)

    async def fail(self, run_id: str, error: str) -> None:
        async with self._lock:
            record = self._runs[run_id]
            record.status = RunStatus.failed
            record.current_node = None
            record.error = error
            record.finished_at = datetime.now(UTC)
