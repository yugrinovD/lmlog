# syntax=docker/dockerfile:1
FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

ARG INSTALL_SERVE=false

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && if [ "$INSTALL_SERVE" = "true" ]; then \
         pip wheel --no-cache-dir --wheel-dir /wheels ".[serve]"; \
       else \
         pip wheel --no-cache-dir --wheel-dir /wheels .; \
       fi


FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 lmlog \
    && useradd --uid 10001 --gid 10001 --create-home lmlog \
    && mkdir -p /data/logs /state /serve \
    && chown -R lmlog:lmlog /data /state /serve

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

USER lmlog
WORKDIR /data

ENTRYPOINT ["lmlog"]
CMD ["--help"]
