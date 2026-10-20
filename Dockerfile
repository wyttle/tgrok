FROM python:3.12-slim

WORKDIR /app

# omp：深入调研（RESEARCH_MODELS）的研究员进程。不用这个功能可以 --build-arg OMP_VERSION= 跳过安装
ARG OMP_VERSION=18.10.3
RUN if [ -n "$OMP_VERSION" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends curl ca-certificates \
        && curl -fsSL https://omp.sh/install | PI_INSTALL_DIR=/usr/local/bin sh -s -- --binary --ref "v${OMP_VERSION}" \
        && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY tgrok/ ./tgrok/

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
