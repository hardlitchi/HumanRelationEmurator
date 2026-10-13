# hre-mcp（エージェント用 MCP サーバー）と hre-web（人のための Web UI）の共通イメージ
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
# hre-admin init-db が ../db を参照するので、server/ と db/ を同じ並びで置いて editable で入れる
COPY db/ db/
COPY server/ server/
RUN pip install -e ./server \
 && useradd --system --uid 10001 --no-create-home hre

USER hre
EXPOSE 8000 8001
CMD ["hre-web", "--host", "0.0.0.0", "--port", "8001"]
