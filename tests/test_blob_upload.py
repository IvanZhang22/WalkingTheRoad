from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.blob_upload import (
    BlobGenerateTokenRequest,
    create_client_upload_token,
    delete_blob,
    validate_managed_blob_url,
)

READ_WRITE_TOKEN = "vercel_blob_rw_store123_testsecret"
PATHNAME = "xingxiaodao-uploads/12345678-1234-1234-1234-123456789abc-recording.mp3"


def request(*, size: int = 18_900_000, content_type: str = "audio/mpeg") -> BlobGenerateTokenRequest:
    return BlobGenerateTokenRequest.model_validate(
        {
            "type": "blob.generate-client-token",
            "payload": {
                "pathname": PATHNAME,
                "clientPayload": json.dumps(
                    {
                        "filename": "recording.mp3",
                        "sizeBytes": size,
                        "contentType": content_type,
                    }
                ),
                "multipart": False,
            },
        }
    )


def decode_client_token(token: str) -> dict[str, object]:
    secured = token.split("_", 4)[4]
    signature_and_payload = base64.b64decode(secured).decode("ascii")
    encoded_payload = signature_and_payload.split(".", 1)[1]
    return json.loads(base64.b64decode(encoded_payload))


def test_generates_scoped_token_for_eighteen_megabyte_audio() -> None:
    response = create_client_upload_token(
        request(), read_write_token=READ_WRITE_TOKEN, max_bytes=100 * 1024 * 1024, now=1000
    )
    assert response["type"] == "blob.generate-client-token"
    assert response["clientToken"].startswith("vercel_blob_client_store123_")
    payload = decode_client_token(response["clientToken"])
    assert payload["pathname"] == PATHNAME
    assert payload["maximumSizeInBytes"] == 100 * 1024 * 1024
    assert payload["validUntil"] == 1_300_000
    assert payload["addRandomSuffix"] is True
    assert "audio/mpeg" in payload["allowedContentTypes"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (request(size=101 * 1024 * 1024), "超过 100 MB"),
        (request(content_type="image/png"), "Content-Type"),
        (
            BlobGenerateTokenRequest.model_validate(
                {
                    "type": "blob.generate-client-token",
                    "payload": {
                        "pathname": "other/recording.mp3",
                        "clientPayload": json.dumps(
                            {
                                "filename": "recording.mp3",
                                "sizeBytes": 1024,
                                "contentType": "audio/mpeg",
                            }
                        ),
                    },
                }
            ),
            "目录无效",
        ),
    ],
)
def test_rejects_unsafe_or_oversized_token_requests(
    payload: BlobGenerateTokenRequest, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        create_client_upload_token(
            payload, read_write_token=READ_WRITE_TOKEN, max_bytes=100 * 1024 * 1024
        )


def test_accepts_only_managed_public_blob_url() -> None:
    url = f"https://abc.public.blob.vercel-storage.com/{PATHNAME}-suffix.mp3"
    assert validate_managed_blob_url(url, filename="recording.mp3") == url
    with pytest.raises(ValueError):
        validate_managed_blob_url("https://files.example.org/recording.mp3", filename="recording.mp3")
    with pytest.raises(ValueError):
        validate_managed_blob_url(
            f"https://abc.public.blob.vercel-storage.com/{PATHNAME}-suffix.mp3?token=x",
            filename="recording.mp3",
        )


async def test_delete_blob_sends_server_token_without_exposing_it_in_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    url = f"https://abc.public.blob.vercel-storage.com/{PATHNAME}-suffix.mp3"
    await delete_blob(
        url,
        read_write_token=READ_WRITE_TOKEN,
        transport=httpx.MockTransport(handler),
    )
    assert len(seen) == 1
    assert seen[0].url == "https://vercel.com/api/blob/delete"
    assert seen[0].headers["authorization"] == f"Bearer {READ_WRITE_TOKEN}"
    assert json.loads(seen[0].content) == {"urls": [url]}
