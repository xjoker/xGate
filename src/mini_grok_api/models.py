"""模型注册表。模型列表完全由 mini.toml [[models.chat]] 驱动，启动时由 main.py 注入。"""

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


# 运行时动态列表，由 set_models() 填充
_models: list[ModelSpec] = []
_BY_ID: dict[str, ModelSpec] = {}


def set_models(specs: list[ModelSpec]) -> None:
    """全量替换模型列表（启动时 + 管理接口更新时调用）。"""
    global _models, _BY_ID
    _models = list(specs)
    _BY_ID = {item.model_id: item for item in _models}


def get_model(model_id: str) -> ModelSpec | None:
    return _BY_ID.get(model_id)


def get_model_specs() -> list[ModelSpec]:
    """返回当前注册的全部模型 spec 列表（只读副本）。"""
    return list(_models)


def _model_dict(spec: ModelSpec, created: int) -> dict:
    return {
        "id": spec.model_id,
        "object": "model",
        "created": created,
        "owned_by": "xai",
        "name": spec.name,
        "image_model": spec.image_model,
        "permission": [],
        "root": spec.model_id,
        "parent": None,
    }


def list_models() -> list[dict]:
    created = int(time.time())
    return [_model_dict(item, created) for item in _models]


def model_to_openai(spec: ModelSpec) -> dict:
    return _model_dict(spec, int(time.time()))
