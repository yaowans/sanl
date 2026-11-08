FROM python:3.11-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates tar \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 subs-check 二进制（独立层：项目代码变动不会触发重新下载 117MB）
# 脚本需留在 scripts/ 下——它按自身路径定位安装目录（INSTALL_DIR=dirname/..）
COPY scripts/install_subscheck.sh ./scripts/install_subscheck.sh
RUN chmod +x scripts/install_subscheck.sh && ./scripts/install_subscheck.sh

# 项目代码
COPY . .

# 数据与输出目录
RUN mkdir -p data output

EXPOSE 8899

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s \
    CMD curl -fsS http://127.0.0.1:8899/api/nodes/stats || exit 1

CMD ["python", "main.py"]
