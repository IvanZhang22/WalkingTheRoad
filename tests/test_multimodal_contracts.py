from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.openai_compat import ChatCompletionRequest


def request_with(part: dict[str, object]) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "分析"}, part]}]}
    )


def test_accepts_audio_image_and_document_url_contracts() -> None:
    audio = request_with(
        {
            "type": "input_audio",
            "input_audio": {"url": "https://files.example.org/a.mp3", "format": "mp3"},
        }
    )
    image = request_with(
        {
            "type": "file",
            "file": {"url": "https://files.example.org/a.png", "filename": "现场照片.png"},
        }
    )
    document = request_with(
        {
            "type": "file",
            "file": {"url": "https://files.example.org/a.pdf", "filename": "材料.pdf"},
        }
    )
    assert len(audio.messages) == len(image.messages) == len(document.messages) == 1


def test_accepts_openai_image_url_contract() -> None:
    image = request_with(
        {
            "type": "image_url",
            "image_url": {
                "url": "https://files.example.org/field-note.png?signature=private",
                "detail": "high",
            },
        }
    )
    assert len(image.messages) == 1


@pytest.mark.parametrize(
    "url",
    [
        "data:audio/wav;base64,AAAA",
        "file:///tmp/interview.wav",
        "http://localhost/interview.wav",
        "http://127.0.0.1/interview.wav",
        "http://169.254.169.254/latest/meta-data",
        "https://user:password@files.example.org/interview.wav",
    ],
)
def test_rejects_non_public_or_credentialed_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        request_with(
            {
                "type": "input_audio",
                "input_audio": {"url": url, "format": "wav"},
            }
        )


def test_rejects_mismatched_audio_and_unsafe_filename() -> None:
    with pytest.raises(ValidationError, match="不一致"):
        request_with(
            {
                "type": "input_audio",
                "input_audio": {
                    "url": "https://files.example.org/interview.mp3",
                    "format": "wav",
                },
            }
        )
    with pytest.raises(ValidationError, match="安全文件名"):
        request_with(
            {
                "type": "file",
                "file": {
                    "url": "https://files.example.org/material.pdf",
                    "filename": "../material.pdf",
                },
            }
        )


def test_only_user_messages_can_carry_attachments_and_video_is_out_of_scope() -> None:
    with pytest.raises(ValidationError, match="只有 user"):
        ChatCompletionRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "file",
                                "file": {
                                    "url": "https://files.example.org/a.pdf",
                                    "filename": "a.pdf",
                                },
                            }
                        ],
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        request_with(
            {
                "type": "input_video",
                "input_video": {"url": "https://files.example.org/a.mp4"},
            }
        )


def test_limits_each_user_message_to_five_attachments() -> None:
    attachments = [
        {
            "type": "file",
            "file": {
                "url": f"https://files.example.org/{index}.txt",
                "filename": f"{index}.txt",
            },
        }
        for index in range(6)
    ]
    with pytest.raises(ValidationError, match="最多包含 5 个附件"):
        ChatCompletionRequest.model_validate(
            {"messages": [{"role": "user", "content": attachments}]}
        )
