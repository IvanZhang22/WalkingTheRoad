"""Durable audio transcription jobs for Qingxiaoda conversations.

The queue is intentionally small: it makes upload acknowledgement quick and
keeps long ASR calls outside the platform's chat request timeout.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.llm import LLMClient
from app.multimodal.contracts import InputAudioContentPart
from app.multimodal.downloader import SafeDownloader
from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile
from app.multimodal.providers.base import ASRProvider


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AudioJob:
    job_id: str
    session_id: str
    filename: str
    status: str
    segment_count: int
    completed_count: int
    failed_count: int
    first_preview: str
    processed_preview: str
    transcript: str
    error: str
    created_at: str


class AudioJobStore:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = RLock()
        with self._connection:
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS audio_jobs (
                    job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, filename TEXT NOT NULL,
                    source_url TEXT NOT NULL, source_format TEXT NOT NULL, raw_path TEXT NOT NULL,
                    status TEXT NOT NULL, segment_count INTEGER NOT NULL DEFAULT 0,
                    completed_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0,
                    first_preview TEXT NOT NULL DEFAULT '', transcript TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', review_confirmed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audio_segments (
                    job_id TEXT NOT NULL, segment_index INTEGER NOT NULL, path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    transcript TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(job_id, segment_index)
                );
            """)
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(audio_jobs)")}
            if "processed_preview" not in columns:
                self._connection.execute(
                    "ALTER TABLE audio_jobs ADD COLUMN processed_preview TEXT NOT NULL DEFAULT ''"
                )

    def create(
        self, session_id: str, attachment: InputAudioContentPart, raw_path: Path
    ) -> AudioJob:
        job_id = f"A-{uuid4().hex[:6].upper()}"
        now = _now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO audio_jobs(job_id,session_id,filename,source_url,source_format,raw_path,status,segment_count,completed_count,failed_count,first_preview,processed_preview,transcript,error,review_confirmed,created_at,updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, 0, 0, '', '', '', '', 0, ?, ?)",
                (
                    job_id,
                    session_id,
                    Path(attachment.input_audio.url.split("?", 1)[0]).name or "audio",
                    attachment.input_audio.url,
                    attachment.input_audio.format,
                    str(raw_path),
                    now,
                    now,
                ),
            )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> AudioJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT job_id,session_id,filename,status,segment_count,completed_count,failed_count,first_preview,processed_preview,transcript,error,created_at FROM audio_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return AudioJob(*row) if row else None

    def update(self, job_id: str, **values: object) -> None:
        if not values:
            return
        values["updated_at"] = _now()
        columns = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE audio_jobs SET {columns} WHERE job_id=?", (*values.values(), job_id)
            )

    def replace_segments(self, job_id: str, paths: list[Path]) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM audio_segments WHERE job_id=?", (job_id,))
            self._connection.executemany(
                "INSERT INTO audio_segments(job_id,segment_index,path,status) VALUES(?,?,?,'queued')",
                [(job_id, i, str(path)) for i, path in enumerate(paths)],
            )
        self.update(job_id, segment_count=len(paths), completed_count=0, failed_count=0)

    def pending_segments(self, job_id: str) -> list[tuple[int, Path, int]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT segment_index,path,attempts FROM audio_segments WHERE job_id=? AND status IN ('queued','failed') ORDER BY segment_index",
                (job_id,),
            ).fetchall()
        return [(int(i), Path(p), int(a)) for i, p, a in rows]

    def set_segment(
        self,
        job_id: str,
        index: int,
        *,
        status: str,
        attempts: int,
        transcript: str = "",
        error: str = "",
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE audio_segments SET status=?,attempts=?,transcript=?,error=? WHERE job_id=? AND segment_index=?",
                (status, attempts, transcript, error[:600], job_id, index),
            )

    def completed_text(self, job_id: str) -> str:
        with self._lock:
            rows = self._connection.execute(
                "SELECT transcript FROM audio_segments WHERE job_id=? AND status='succeeded' ORDER BY segment_index",
                (job_id,),
            ).fetchall()
        return "\n\n".join(str(row[0]).strip() for row in rows if str(row[0]).strip())

    def failure_summary(self, job_id: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT error FROM audio_segments WHERE job_id=? AND status='failed' AND error<>'' "
                "ORDER BY segment_index LIMIT 1",
                (job_id,),
            ).fetchone()
        return str(row[0])[:300] if row else "部分分段转写失败。"

    def cleanup(self, raw_hours: int, transcript_days: int) -> None:
        cutoff = datetime.now(UTC)
        with self._lock:
            rows = self._connection.execute(
                "SELECT job_id,raw_path,created_at,status FROM audio_jobs"
            ).fetchall()
        for job_id, raw_path, created_at, status in rows:
            try:
                created = datetime.fromisoformat(created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
            except ValueError:
                continue
            if status in {"succeeded", "failed", "cancelled"} and created < cutoff - timedelta(
                hours=raw_hours
            ):
                shutil.rmtree(Path(raw_path).parent, ignore_errors=True)
            if created < cutoff - timedelta(days=transcript_days):
                with self._lock, self._connection:
                    self._connection.execute("DELETE FROM audio_segments WHERE job_id=?", (job_id,))
                    self._connection.execute("DELETE FROM audio_jobs WHERE job_id=?", (job_id,))


class AudioJobService:
    def __init__(
        self,
        *,
        store: AudioJobStore,
        provider: ASRProvider | None,
        storage_dir: Path,
        segment_seconds: int = 300,
        concurrency: int = 3,
        max_segments: int = 24,
        max_chars: int = 12000,
        llm: LLMClient | None = None,
    ) -> None:
        self.store, self.provider, self.storage_dir = store, provider, storage_dir
        self.segment_seconds, self.concurrency, self.max_segments, self.max_chars = (
            segment_seconds,
            concurrency,
            max_segments,
            max_chars,
        )
        self.llm = llm
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(
        self, session_id: str, attachment: InputAudioContentPart, downloader: SafeDownloader
    ) -> AudioJob:
        source = await downloader.download(attachment)
        job_dir = self.storage_dir / uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=True)
        raw_path = job_dir / f"source.{attachment.input_audio.format}"
        shutil.copy2(source.path, raw_path)
        source.path.unlink(missing_ok=True)
        job = self.store.create(session_id, attachment, raw_path)
        self.start(job.job_id)
        return job

    def start(self, job_id: str) -> None:
        task = asyncio.create_task(self._run(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or self.provider is None:
            if job:
                self.store.update(job_id, status="failed", error="音频转写服务尚未配置。")
            return
        try:
            self.store.update(job_id, status="preparing", error="")
            pending = self.store.pending_segments(job_id)
            if not pending:
                paths = await asyncio.to_thread(self._split, Path(self._raw_path(job_id)))
                self.store.replace_segments(job_id, paths)
                pending = self.store.pending_segments(job_id)
            self.store.update(job_id, status="transcribing")
            semaphore = asyncio.Semaphore(self.concurrency)
            await asyncio.gather(
                *(
                    self._transcribe_one(job_id, i, path, attempts, semaphore)
                    for i, path, attempts in pending
                )
            )
            text = self.store.completed_text(job_id)
            failures = len(self.store.pending_segments(job_id))
            completed = len(text and [x for x in text.split("\n\n") if x] or [])
            if failures:
                self.store.update(
                    job_id,
                    status="failed",
                    failed_count=failures,
                    completed_count=completed,
                    error=self.store.failure_summary(job_id),
                )
            else:
                raw_preview = text[:4000]
                self.store.update(
                    job_id,
                    status="awaiting_review",
                    completed_count=completed,
                    transcript=text,
                    first_preview=raw_preview,
                    processed_preview=await self._prepare_preview(raw_preview),
                )
        except Exception as exc:
            self.store.update(
                job_id, status="failed", error=f"音频处理未完成：{type(exc).__name__}"
            )

    async def _prepare_preview(self, raw_preview: str) -> str:
        """Return a cautious, readable preview; raw ASR always remains intact."""
        compact = "".join(raw_preview.split())
        if not compact:
            return ""
        if self.llm is None:
            return compact
        prompt = (
            "下面是访谈音频首段的机器转写，可能有漏字、同音字和断句错误。"
            "请只做可读性整理：合并无意义换行，按话题分段；保留口语、犹豫和不确定处，"
            "不补造事实，不把听不清内容改写成确定陈述。输出必须是：\n"
            "# 一、内容提要\n## 1、...\n## 2、...\n\n# 二、整理后的转写\n（分段正文）\n"
            "不要输出解释、免责声明或代码块。\n\n<raw_asr>\n" + compact[:12000] + "\n</raw_asr>"
        )
        try:
            answer = await self.llm.complete(
                node_id="3A-3-4",
                system_prompt="你是谨慎的访谈转写整理助手。",
                user_prompt=prompt,
                temperature=0.1,
            )
            return answer.strip()[:16000] if answer.strip() else compact
        except Exception:
            return compact

    def _raw_path(self, job_id: str) -> str:
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT raw_path FROM audio_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if not row:
            raise ValueError("audio job not found")
        return str(row[0])

    def _split(self, raw_path: Path) -> list[Path]:
        output_dir = raw_path.parent / "segments"
        output_dir.mkdir(exist_ok=True)
        target = output_dir / "part-%03d.mp3"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_seconds),
            "-reset_timestamps",
            "1",
            str(target),
        ]
        subprocess.run(
            command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180
        )
        paths = sorted(output_dir.glob("part-*.mp3"))
        if not paths:
            raise RuntimeError("ffmpeg produced no segments")
        if len(paths) > self.max_segments:
            raise RuntimeError(f"音频超过 {self.max_segments} 个分段上限")
        return paths

    async def _transcribe_one(
        self, job_id: str, index: int, path: Path, attempts: int, semaphore: asyncio.Semaphore
    ) -> None:
        if self.provider is None:
            self.store.set_segment(
                job_id, index, status="failed", attempts=attempts, error="provider_missing"
            )
            return
        async with semaphore:
            for attempt in range(attempts + 1, 3):
                self.store.set_segment(job_id, index, status="transcribing", attempts=attempt)
                source = DownloadedFile(
                    path=path,
                    filename=path.name,
                    mime_type="audio/mpeg",
                    size_bytes=path.stat().st_size,
                    sha256="0" * 64,
                    source_format="mp3",
                )
                try:
                    result = await self.provider.transcribe(source)
                    text = (result.normalized_text or "").strip()
                    if len(text) > self.max_chars:
                        raise MaterialIngestError(
                            "XDW-ASR-SEGMENT-TOO-LONG", "单个音频分段转写超过输入上限。"
                        )
                    self.store.set_segment(
                        job_id, index, status="succeeded", attempts=attempt, transcript=text
                    )
                    return
                except Exception as exc:
                    safe_error = str(exc).replace("\n", " ")[:300] or type(exc).__name__
                    self.store.set_segment(
                        job_id, index, status="failed", attempts=attempt, error=safe_error
                    )
                    if attempt >= 2:
                        return
