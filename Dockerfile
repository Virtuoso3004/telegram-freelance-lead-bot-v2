FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

COPY pyproject.toml uv.lock ./
# pyproject.toml pins uv 0.12.2; the base image ships an older one.
RUN uv self update 0.12.2 && uv sync --locked --no-dev

COPY . .

EXPOSE 10000

CMD ["python", "-m", "freelancer_bot.render_entry"]
