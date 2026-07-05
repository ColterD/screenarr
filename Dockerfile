FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bridge ./bridge

RUN pip install .

EXPOSE 7879

CMD ["uvicorn", "bridge.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "7879"]
