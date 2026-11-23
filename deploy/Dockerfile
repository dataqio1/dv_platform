# Multi-stage build. Runs on any Linux host; no cloud-specific base image.
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[server]"

FROM python:3.12-slim AS runtime
# Non-root user
RUN useradd --create-home --uid 10001 dvp
WORKDIR /app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /app/src /app/src
ENV DVP_PROFILE=server DVP_API_HOST=0.0.0.0 DVP_API_PORT=8000
USER dvp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health').status==200 else 1)"
CMD ["dvp", "run"]
