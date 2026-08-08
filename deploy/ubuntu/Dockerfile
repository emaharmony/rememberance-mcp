FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RECALL_HOME=/var/lib/recall

RUN useradd --create-home --uid 10001 recall \
    && install -d -o recall -g recall /var/lib/recall /app

WORKDIR /app
COPY pyproject.toml README.md MANIFEST.in LICENSE ./
COPY --chmod=755 deploy/ubuntu/entrypoint.sh /usr/local/bin/recall-entrypoint

COPY src ./src
RUN python -m pip install --upgrade pip "setuptools>=83" wheel \
    && python -m pip install ".[all]"

USER recall
EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/health/live', timeout=2)"
ENTRYPOINT ["/usr/local/bin/recall-entrypoint"]
