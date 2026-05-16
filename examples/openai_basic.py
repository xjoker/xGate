"""OpenAI Python SDK 直接接入 xGate 跑 chat + image。

依赖: pip install openai>=1.0
环境变量: XGATE_API_KEY = 你 mini.toml 里的 api_key
"""

from __future__ import annotations

import os

from openai import OpenAI

API_KEY = os.environ["XGATE_API_KEY"]
BASE_URL = os.environ.get("XGATE_BASE_URL", "http://127.0.0.1:8024/v1")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)


def demo_chat() -> None:
    """非流式 chat completion。"""
    resp = client.chat.completions.create(
        model="grok-4.20-fast",
        messages=[{"role": "user", "content": "用一句话介绍 xAI 的 Grok"}],
    )
    print("[chat]", resp.choices[0].message.content)


def demo_image() -> None:
    """一次性生图（同步等待第一批返回）。"""
    resp = client.images.generate(
        model="grok-imagine-image-lite",
        prompt="a serene mountain lake at sunrise, photorealistic",
        n=1,
        size="1024x1024",
    )
    print("[image] url=", resp.data[0].url)


def demo_models() -> None:
    """列模型 — 兼容 OpenAI /v1/models。"""
    models = client.models.list()
    for m in models.data[:6]:
        print("[model]", m.id)


if __name__ == "__main__":
    demo_models()
    demo_chat()
    demo_image()
