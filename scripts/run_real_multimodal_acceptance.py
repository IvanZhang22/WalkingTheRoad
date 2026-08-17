from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.multimodal.errors import MaterialIngestError
from app.multimodal.models import DownloadedFile, ProviderResult
from app.multimodal.providers.baidu_ocr import BaiduOCRProvider
from app.multimodal.providers.deepgram_asr import DeepgramASRProvider

PASS_CONFIDENCE = 0.8


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    modality: str
    filename: str
    success: bool
    elapsed_seconds: float
    output_file: str | None
    segment_count: int
    high_confidence_segments: int
    mean_confidence: float | None
    locator_complete: bool
    provider: str | None = None
    model: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real Deepgram and Baidu OCR acceptance tests.")
    parser.add_argument("--audio", type=Path, help="Local MP3/WAV/M4A/WEBM test file.")
    parser.add_argument("--image", type=Path, help="Local PNG/JPG/JPEG/WEBP/PDF test file.")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "private" / "multimodal-acceptance",
    )
    return parser.parse_args()


def downloaded_file(path: Path) -> DownloadedFile:
    raw = path.read_bytes()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return DownloadedFile(
        path=path,
        source_format=path.suffix.lower().lstrip("."),
        filename=path.name,
        mime_type=mime_type,
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def result_metrics(result: ProviderResult, modality: str) -> tuple[int, int, float | None, bool]:
    confidences = [segment.confidence for segment in result.segments if segment.confidence is not None]
    high_confidence = sum(value >= PASS_CONFIDENCE for value in confidences)
    mean_confidence = statistics.fmean(confidences) if confidences else None
    if modality == "audio":
        locator_complete = all(
            segment.locator.start_ms is not None and segment.locator.end_ms is not None
            for segment in result.segments
        )
    else:
        locator_complete = all(segment.locator.bbox is not None for segment in result.segments)
    return len(result.segments), high_confidence, mean_confidence, locator_complete


async def run_provider(
    *,
    modality: str,
    path: Path,
    output_dir: Path,
    provider: DeepgramASRProvider | BaiduOCRProvider,
) -> AcceptanceResult:
    started = time.perf_counter()
    output_file = output_dir / f"{modality}_provider_result.json"
    try:
        source = downloaded_file(path)
        if modality == "audio":
            assert isinstance(provider, DeepgramASRProvider)
            result = await provider.transcribe(source)
        else:
            assert isinstance(provider, BaiduOCRProvider)
            result = await provider.recognize(source)
        elapsed = time.perf_counter() - started
        output_file.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        segment_count, high_confidence, mean_confidence, locator_complete = result_metrics(
            result, modality
        )
        return AcceptanceResult(
            modality=modality,
            filename=path.name,
            success=True,
            elapsed_seconds=elapsed,
            output_file=str(output_file),
            segment_count=segment_count,
            high_confidence_segments=high_confidence,
            mean_confidence=mean_confidence,
            locator_complete=locator_complete,
            provider=result.provider_name,
            model=result.provider_model,
        )
    except MaterialIngestError as exc:
        return AcceptanceResult(
            modality=modality,
            filename=path.name,
            success=False,
            elapsed_seconds=time.perf_counter() - started,
            output_file=None,
            segment_count=0,
            high_confidence_segments=0,
            mean_confidence=None,
            locator_complete=False,
            error_code=exc.code,
            error_message=exc.public_message,
        )
    except Exception as exc:
        return AcceptanceResult(
            modality=modality,
            filename=path.name,
            success=False,
            elapsed_seconds=time.perf_counter() - started,
            output_file=None,
            segment_count=0,
            high_confidence_segments=0,
            mean_confidence=None,
            locator_complete=False,
            error_code=type(exc).__name__,
            error_message=str(exc),
        )


def markdown_report(results: list[AcceptanceResult], env_file: Path) -> str:
    rows = []
    for item in results:
        mean = f"{item.mean_confidence:.4f}" if item.mean_confidence is not None else "n/a"
        status = "PASS" if item.success and item.locator_complete else "FAIL"
        details = item.error_code or f"{item.high_confidence_segments}/{item.segment_count} >= 0.8"
        rows.append(
            f"| {item.modality} | {item.provider or '-'} | {item.model or '-'} | {status} | "
            f"{item.elapsed_seconds:.3f}s | {item.segment_count} | {mean} | "
            f"{'yes' if item.locator_complete else 'no'} | {details} |"
        )
    return "\n".join(
        [
            "# Real Multimodal Acceptance",
            "",
            f"- Run at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"- Environment file: `{env_file.name}` (credential values were not recorded)",
            f"- Confidence gate: `{PASS_CONFIDENCE}`",
            "",
            "| Modality | Provider | Model | Status | Elapsed | Segments | Mean confidence | Locator complete | Details |",
            "|---|---|---|---:|---:|---:|---:|---:|---|",
            *rows,
            "",
            "Provider outputs are local artifacts and are excluded from Git by `private/`.",
        ]
    )


async def async_main() -> int:
    args = parse_args()
    load_dotenv(args.env_file, override=True)
    settings = get_settings()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[AcceptanceResult] = []

    if args.audio:
        if not args.audio.is_file():
            raise FileNotFoundError(args.audio)
        results.append(
            await run_provider(
                modality="audio",
                path=args.audio,
                output_dir=args.output_dir,
                provider=DeepgramASRProvider(
                    api_key=settings.deepgram_api_key,
                    base_url=settings.deepgram_base_url,
                    model=settings.deepgram_model,
                    language=settings.deepgram_language,
                    diarize_model=settings.deepgram_diarize_model,
                    timeout=settings.deepgram_timeout_seconds,
                ),
            )
        )

    if args.image:
        if not args.image.is_file():
            raise FileNotFoundError(args.image)
        results.append(
            await run_provider(
                modality="image",
                path=args.image,
                output_dir=args.output_dir,
                provider=BaiduOCRProvider(
                    api_key=settings.baidu_ocr_api_key,
                    secret_key=settings.baidu_ocr_secret_key,
                    base_url=settings.baidu_ocr_base_url,
                    endpoint_path=settings.baidu_ocr_endpoint_path,
                    timeout=settings.baidu_ocr_timeout_seconds,
                    max_pages=settings.baidu_ocr_max_pages,
                ),
            )
        )

    if not results:
        raise ValueError("At least one of --audio or --image is required.")

    summary_file = args.output_dir / "summary.json"
    summary_file.write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_file = args.output_dir / "report.md"
    report_file.write_text(markdown_report(results, args.env_file), encoding="utf-8")

    for item in results:
        print(
            f"{item.modality}: success={item.success}, provider={item.provider}, "
            f"segments={item.segment_count}, elapsed={item.elapsed_seconds:.3f}s, "
            f"error={item.error_code or '-'}"
        )
    print(f"summary={summary_file}")
    print(f"report={report_file}")
    return 0 if all(item.success and item.locator_complete for item in results) else 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
