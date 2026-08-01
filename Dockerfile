FROM node:22-alpine AS frontend-build

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm --prefix frontend ci

COPY frontend ./frontend
COPY app ./app
RUN npm --prefix frontend run build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir "uv==0.11.7"

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY --from=frontend-build /build/app/static ./app/static

RUN uv sync --frozen --no-dev

RUN mkdir -p /app/data

EXPOSE 1456

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "1456"]
