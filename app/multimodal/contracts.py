"""OpenAI/清小搭多模态 content part 的严格输入契约。"""

from __future__ import annotations

import ipaddress
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AUDIO_FORMATS = frozenset({"mp3", "wav", "m4a", "webm"})
DOCUMENT_FORMATS = frozenset({"txt", "md", "docx", "pdf", "xlsx"})
IMAGE_FORMATS = frozenset({"png", "jpg", "jpeg", "webp"})
FILE_FORMATS = DOCUMENT_FORMATS | IMAGE_FORMATS
MAX_ATTACHMENTS_PER_MESSAGE = 5


def _validate_public_http_url(value: str) -> str:
    url = value.strip()
    if not url or len(url) > 4096:
        raise ValueError("附件 URL 缺失或过长")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("附件只接受可下载的 http/https URL")
    if parsed.username or parsed.password:
        raise ValueError("附件 URL 不得包含用户名或密码")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("附件 URL 不得指向本机或内网地址")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return url
    if not address.is_global:
        raise ValueError("附件 URL 不得指向本机、内网或元数据地址")
    return url


def _safe_filename(value: str) -> str:
    filename = value.strip()
    normalized = filename.replace("\\", "/")
    if (
        not filename
        or len(filename) > 300
        or "\x00" in filename
        or PurePosixPath(normalized).name != normalized
        or normalized in {".", ".."}
    ):
        raise ValueError("filename 必须是不含路径的安全文件名")
    return filename


def _url_suffix(url: str) -> str:
    path = unquote(urlsplit(url).path)
    return PurePosixPath(path).suffix.lower().lstrip(".")


class TextContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["text"]
    text: str = Field(min_length=1, max_length=20_000)


class InputAudioReference(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str
    format: Literal["mp3", "wav", "m4a", "webm"]

    @field_validator("url")
    @classmethod
    def public_http_url(cls, value: str) -> str:
        return _validate_public_http_url(value)

    @model_validator(mode="after")
    def extension_matches_format(self) -> InputAudioReference:
        suffix = _url_suffix(self.url)
        if suffix in AUDIO_FORMATS and suffix != self.format:
            raise ValueError("音频 URL 扩展名与 format 不一致")
        return self


class InputAudioContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_audio"]
    input_audio: InputAudioReference


class ImageUrlReference(BaseModel):
    """兼容 OpenAI ``image_url``，以及清小搭可能额外附带的展示字段。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    url: str
    filename: str | None = None

    @field_validator("url")
    @classmethod
    def public_http_url(cls, value: str) -> str:
        return _validate_public_http_url(value)

    @field_validator("filename")
    @classmethod
    def safe_image_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        filename = _safe_filename(value)
        suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
        if suffix not in IMAGE_FORMATS:
            raise ValueError("图片 filename 仅支持 png、jpg、jpeg 或 webp")
        return filename


class ImageUrlContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["image_url"]
    image_url: ImageUrlReference


class FileReference(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str
    filename: str

    @field_validator("url")
    @classmethod
    def public_http_url(cls, value: str) -> str:
        return _validate_public_http_url(value)

    @field_validator("filename")
    @classmethod
    def safe_supported_filename(cls, value: str) -> str:
        filename = _safe_filename(value)
        suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
        if suffix not in FILE_FORMATS:
            supported = "、".join(sorted(FILE_FORMATS))
            raise ValueError(f"当前文件类型不受支持；仅支持 {supported}")
        return filename


class FileContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    file: FileReference


type ContentPart = Annotated[
    TextContentPart | InputAudioContentPart | ImageUrlContentPart | FileContentPart,
    Field(discriminator="type"),
]
type ContentPartList = Annotated[list[ContentPart], Field(min_length=1, max_length=12)]
type ChatContent = Annotated[str, Field(min_length=1, max_length=20_000)] | ContentPartList


def attachment_parts(
    content: ChatContent,
) -> list[InputAudioContentPart | ImageUrlContentPart | FileContentPart]:
    if isinstance(content, str):
        return []
    return [
        part
        for part in content
        if isinstance(part, (InputAudioContentPart, ImageUrlContentPart, FileContentPart))
    ]


def text_from_content(content: ChatContent) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part.text for part in content if isinstance(part, TextContentPart)).strip()


def validate_attachment_count(content: ChatContent) -> None:
    if len(attachment_parts(content)) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ValueError(f"单条消息最多包含 {MAX_ATTACHMENTS_PER_MESSAGE} 个附件")
