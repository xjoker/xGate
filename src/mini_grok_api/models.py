"""模型注册表，MVP 先使用固定清单。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model_id: str
    mode_id: str
    name: str
    image_model: bool = field(default=False)
    enable_pro: bool = field(default=False)


MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("grok-4.20-fast", "fast", "Grok 4.20 Fast"),
    ModelSpec("grok-4.20-auto", "auto", "Grok 4.20 Auto"),
    ModelSpec("grok-4.20-expert", "expert", "Grok 4.20 Expert"),
    ModelSpec("grok-4.20-heavy", "heavy", "Grok 4.20 Heavy"),
    ModelSpec("grok-4.3-beta", "grok-420-computer-use-sa", "Grok 4.3 Beta"),
    ModelSpec("grok-imagine", "imagine", "Grok Imagine (Speed)", image_model=True, enable_pro=False),
    ModelSpec("grok-imagine-pro", "imagine", "Grok Imagine Pro (Quality)", image_model=True, enable_pro=True),
)

_BY_ID = {item.model_id: item for item in MODELS}


def get_model(model_id: str) -> ModelSpec | None:
    return _BY_ID.get(model_id)


def _model_dict(spec: ModelSpec, created: int) -> dict:
    return {
        "id": spec.model_id,
        "object": "model",
        "created": created,
        "owned_by": "xai",
        "name": spec.name,
        "permission": [],
        "root": spec.model_id,
        "parent": None,
    }


def list_models() -> list[dict]:
    created = int(time.time())
    return [_model_dict(item, created) for item in MODELS]


def model_to_openai(spec: ModelSpec) -> dict:
    return _model_dict(spec, int(time.time()))
