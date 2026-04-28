"""OpenAI 兼容请求模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MessageItem(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[MessageItem]
    stream: bool | None = False
    temperature: float | None = Field(default=0.8, ge=0, le=2)
    top_p: float | None = Field(default=0.95, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: str | None = None


class ImageGenerationRequest(BaseModel):
    model: str = "grok-imagine"
    prompt: str
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: str = "url"


class ImageStreamStartRequest(BaseModel):
    prompt: str
    model: str = "grok-imagine"
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    interval_seconds: float = Field(default=5.0, ge=1.0, le=3600.0)
    max_rounds: int = Field(default=-1, ge=-1)
    image_data: str | None = None


class TaskQueueAddRequest(BaseModel):
    prompt: str
    target_count: int = Field(default=10, ge=1, le=1000)
    size: str = "1024x1024"
    model: str = "grok-imagine"
    interval_seconds: float = Field(default=5.0, ge=1.0, le=3600.0)


class VideoGenerationRequest(BaseModel):
    prompt: str
    resolution: str = "480p"
    duration: str = "6s"
    aspect_ratio: str = "2:3"
    image_data: str | None = None


class OpenAIError(BaseModel):
    message: str
    type: str = "server_error"
    code: str | None = None
    param: str | None = None


class OpenAIErrorResponse(BaseModel):
    error: OpenAIError
