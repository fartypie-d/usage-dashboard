# ===== Stage 1: builder =====
FROM python:3.12-slim AS builder
WORKDIR /build

# 시스템 의존성 최소화 (build only)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 복사 → cache 활용
COPY pyproject.toml README.md* ./
COPY app/ ./app/

# 최종 requirements freeze — runtime install에 활용
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && pip freeze | grep -vE "^(pip|setuptools|wheel|-e |usage[-_]dashboard\b|usage[-_]dashboard @)" > /requirements-lock.txt

# ===== Stage 2: runtime =====
FROM python:3.12-slim AS runtime
WORKDIR /app

# non-root user
RUN useradd -r -u 1001 -m dashuser

# freeze된 의존성만 설치 (앱 소스는 site-packages에 설치하지 않음)
COPY --from=builder /requirements-lock.txt /tmp/requirements-lock.txt
RUN pip install --no-cache-dir -r /tmp/requirements-lock.txt \
    && rm /tmp/requirements-lock.txt

# 앱 소스 (명시적 COPY — /app/app 경로에서 실행)
COPY --chown=dashuser:dashuser app/ /app/app/

# static 파일 별도 복사 (setuptools 패키지에 미포함)
COPY --chown=dashuser:dashuser static/ /app/static/

USER dashuser

EXPOSE 9280

# 컨테이너 내부는 0.0.0.0, 호스트 노출은 compose에서 127.0.0.1로 바인딩
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9280"]
