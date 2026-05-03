FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/shopee-agent

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY pyproject.toml README.md ./
COPY common ./common
COPY core ./core
COPY modules ./modules
COPY web ./web

RUN pip install --no-cache-dir --no-build-isolation \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    . && playwright install --with-deps chromium

COPY accounts ./accounts
COPY farm ./farm
COPY memory ./memory
COPY config.yaml .
COPY .env.example .env.example

EXPOSE 8080
