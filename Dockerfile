FROM python:3.13-slim

ENV HOME=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/meshtui

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir ".[mqtt]"

ENTRYPOINT ["meshtui"]
CMD ["gateway", "--help"]
