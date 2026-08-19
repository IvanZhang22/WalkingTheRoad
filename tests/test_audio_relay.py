from __future__ import annotations

import hashlib
from pathlib import Path

from app.multimodal.audio_relay import TemporaryAudioRelay
from app.multimodal.models import DownloadedFile


async def test_relay_publishes_wav_as_a_short_lived_https_url(tmp_path: Path) -> None:
    path = tmp_path / "interview.wav"
    path.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    source = DownloadedFile(
        path=path,
        source_format="wav",
        filename="interview.wav",
        mime_type="audio/wav",
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    relay = TemporaryAudioRelay(public_base_url="https://agent.example.org")
    published, lease = await relay.publish(source)
    assert published.source_url == f"https://agent.example.org/api/internal/asr-audio/{lease.token}"
    assert await relay.take_path(lease.token) == (path, "audio/wav")
    await relay.revoke(lease)
    assert await relay.take_path(lease.token) is None
