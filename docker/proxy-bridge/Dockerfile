FROM --platform=linux/amd64 python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 TZ=UTC

COPY http_proxy.py /http_proxy.py

EXPOSE 8118

# 入口：通过 PROXY_URL 环境变量传 socks5://user:pass@host:port，监听 0.0.0.0:8118
CMD ["sh", "-c", "exec python3 /http_proxy.py \"${PROXY_URL}\" 8118 0.0.0.0"]
