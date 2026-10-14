FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
    TZ=Asia/Shanghai \
    WEB_PORT=8600 \
    DATA_DIR=/data

# 国内 apt 源（bookworm 用 debian.sources；镜像源全换成清华，装 chromium 依赖更快）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org/debian-security|mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt \
 && pip install "fastapi>=0.115,<0.116" "uvicorn[standard]>=0.32,<0.35"

# Chromium 运行所需的系统库 + 浏览器本体（~600MB，构建时一次性完成）
# 顺带装 emoji / 中文字体：失败截图里的表情和中文不会变成方块
RUN python -m playwright install-deps chromium \
 && apt-get install -y --no-install-recommends fonts-noto-color-emoji fonts-noto-cjk \
 && python -m playwright install chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .

RUN mkdir -p /data/secrets /data/artifacts \
 && cp deploy/docker/config.default.json /app/config.default.json \
 && chmod +x deploy/docker/entrypoint.sh

EXPOSE 8600
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"WEB_PORT\",\"8600\")}/healthz',timeout=4)"

CMD ["bash", "deploy/docker/entrypoint.sh"]
