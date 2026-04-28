# syntax=docker/dockerfile:1.6
FROM python:3.13-slim AS build

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    TZ=UTC \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim AS runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    TZ=UTC

COPY --from=build /install /usr/local

# 运行时数据目录由 docker-compose volume 挂载（./data:/app/data）
# 这里仅创建空目录占位，避免镜像里打包用户的真实 cookie / 配置
RUN mkdir -p /app/data/config /app/data/images /app/data/file
VOLUME ["/app/data"]

EXPOSE 8024

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8024/health',timeout=3).status==200 else 1)"

CMD ["xgate"]
