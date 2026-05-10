"""OpenAI 兼容请求模型。

所有 OpenAI 兼容请求 schema 默认 `extra="ignore"`：未知字段静默丢弃，
保证 OpenAI/LangChain/openai-py 等客户端把任何尚未实现的参数发过来都不会报 422。
显式列出的可选字段并不一定生效（部分仅做接受、不做语义实现），但至少校验通过。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _OpenAIBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MessageItem(_OpenAIBase):
    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class StreamOptions(_OpenAIBase):
    include_usage: bool | None = False


class ChatCompletionRequest(_OpenAIBase):
    model: str
    messages: list[MessageItem]
    stream: bool | None = False
    temperature: float | None = Field(default=0.8, ge=0, le=2)
    top_p: float | None = Field(default=0.95, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: str | None = None
    # 以下字段做兼容性接受，目前不实现语义，避免客户端 422
    n: int | None = Field(default=1, ge=1, le=8)
    stop: str | list[str] | None = None
    seed: int | None = None
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    logit_bias: dict[str, float] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = Field(default=None, ge=0, le=20)
    user: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    store: bool | None = None
    service_tier: str | None = None
    stream_options: StreamOptions | None = None
    modalities: list[str] | None = None


class ImageGenerationRequest(_OpenAIBase):
    model: str | None = None
    prompt: str
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: str = "url"
    quality: str | None = None
    style: str | None = None
    user: str | None = None


class ImageStreamStartRequest(BaseModel):
    prompt: str
    model: str | None = None
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    interval_seconds: float = Field(default=5.0, ge=1.0, le=3600.0)
    max_rounds: int = Field(default=-1, ge=-1)
    max_images: int = Field(default=0, ge=0)  # 0=不限；>0 时按图片数严格停止
    image_data: str | None = None


class TaskQueueAddRequest(BaseModel):
    prompt: str
    target_count: int = Field(default=10, ge=1, le=1000)
    size: str = "1024x1024"
    origin: str = Field(default="queue")  # queue / chat / api，仅用于前端区分展示
    model: str | None = None
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
