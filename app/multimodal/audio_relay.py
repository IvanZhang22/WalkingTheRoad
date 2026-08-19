"""短时公开音频中转：让仅接受 URL 的 ASR 安全读取清小搭上传的文件。"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile

STEPFUN_FORMATS = frozenset({"mp3", "wav", "ogg", "pcm"})
TRANSCODE_FORMATS = frozenset({"m4a", "webm"})


@dataclass(slots=True)
class RelayLease:
    token: str
    path: Path
    mime_type: str
    expires_at: float
    converted_path: Path | None = None


class TemporaryAudioRelay:
    """只在一次 ASR 请求期间公开随机 URL，完成或超时后删除访问记录。"""

    def __init__(
        self,
        *,
        public_base_url: str,
        ttl_seconds: int = 600,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        if not public_base_url.startswith("https://"):
            raise ValueError("ASR_RELAY_PUBLIC_BASE_URL 必须是 HTTPS 公网地址")
        if ttl_seconds <= 0:
            raise ValueError("ASR_RELAY_TTL_SECONDS 必须大于 0")
        self.public_base_url = public_base_url.rstrip("/")
        self.ttl_seconds = ttl_seconds
        self.ffmpeg_path = ffmpeg_path
        self._leases: dict[str, RelayLease] = {}
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.public_base_url)

    async def publish(self, source: DownloadedFile) -> tuple[DownloadedFile, RelayLease]:
        suffix = (source.source_format or source.path.suffix.lstrip(".")).lower()
        published = source
        converted_path: Path | None = None
        if suffix in TRANSCODE_FORMATS:
            converted_path = await asyncio.to_thread(self._transcode_to_mp3, source.path)
            raw = await asyncio.to_thread(converted_path.read_bytes)
            published = source.model_copy(
                update={
                    "path": converted_path,
                    "filename": f"{Path(source.filename).stem}.mp3",
                    "source_format": "mp3",
                    "mime_type": "audio/mpeg",
                    "size_bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        elif suffix not in STEPFUN_FORMATS:
            raise MaterialIngestError(
                "XDW-ASR-FORMAT",
                "当前音频格式不受支持，请上传 mp3、wav、ogg、pcm、m4a 或 webm。",
            )

        token = secrets.token_urlsafe(32)
        lease = RelayLease(
            token=token,
            path=published.path,
            mime_type=published.mime_type,
            expires_at=time.monotonic() + self.ttl_seconds,
            converted_path=converted_path,
        )
        async with self._lock:
            self._purge_expired_locked()
            self._leases[token] = lease
        return published.model_copy(
            update={"source_url": f"{self.public_base_url}/api/internal/asr-audio/{token}"}
        ), lease

    async def take_path(self, token: str) -> tuple[Path, str] | None:
        async with self._lock:
            self._purge_expired_locked()
            lease = self._leases.get(token)
            if lease is None or not lease.path.is_file():
                return None
            return lease.path, lease.mime_type

    async def revoke(self, lease: RelayLease | None) -> None:
        if lease is None:
            return
        async with self._lock:
            self._leases.pop(lease.token, None)
        if lease.converted_path is not None:
            lease.converted_path.unlink(missing_ok=True)

    def _purge_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [token for token, lease in self._leases.items() if lease.expires_at <= now]
        for token in expired:
            lease = self._leases.pop(token)
            if lease.converted_path is not None:
                lease.converted_path.unlink(missing_ok=True)

    def _transcode_to_mp3(self, source: Path) -> Path:
        executable = shutil.which(self.ffmpeg_path)
        if executable is None:
            raise MaterialIngestError(
                "XDW-ASR-TRANSCODER-MISSING",
                "服务器尚未安装音频转码组件，暂不能处理 m4a 或 webm。",
            )
        target = source.with_suffix(".asr.mp3")
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "4",
                    str(target),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            target.unlink(missing_ok=True)
            raise MaterialIngestError(
                "XDW-ASR-TRANSCODE", "音频转码失败，请改传 mp3 或 wav。", retryable=True
            ) from exc
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise MaterialIngestError("XDW-ASR-TRANSCODE", "音频转码失败，请改传 mp3 或 wav。")
        return target
