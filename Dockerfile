# Headless image for scheduled jobs such as `socagents x flow`. Credentials come from the
# environment (there is no OS keychain in a container); state lives in $SOCAGENTS_HOME,
# which should be a mounted volume so cursors survive between runs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SOCAGENTS_HOME=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY socagents ./socagents
RUN pip install ".[flow]" \
    && useradd --uid 10001 --create-home socagents \
    && mkdir -p /data && chown socagents /data

USER socagents
VOLUME ["/data"]
ENTRYPOINT ["socagents"]
CMD ["--help"]
