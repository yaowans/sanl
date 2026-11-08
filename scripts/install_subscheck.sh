#!/usr/bin/env bash
# 下载 subs-check 二进制（beck-8/subs-check, GPL-3.0，以子进程方式隔离调用）
# 用法: ./scripts/install_subscheck.sh [版本]  默认 latest
set -euo pipefail

VERSION="${1:-latest}"
REPO="beck-8/subs-check"
INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$INSTALL_DIR/subs-check"

if [ -x "$BIN" ]; then
    echo "✅ subs-check 已存在: $BIN"
    exit 0
fi

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)  ASSET="Linux_x86_64" ;;
    aarch64|arm64) ASSET="Linux_aarch64" ;;
    *) echo "❌ 不支持的架构: $ARCH"; exit 1 ;;
esac

if [ "$VERSION" = "latest" ]; then
    URL="https://github.com/${REPO}/releases/latest/download/subs-check_${ASSET}.tar.gz"
else
    URL="https://github.com/${REPO}/releases/download/${VERSION}/subs-check_${ASSET}.tar.gz"
fi

echo "⬇️  下载 $URL"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# GitHub 大文件下载偶发 5xx/504，重试若干次再放弃
curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
     -o "$TMP/subs-check.tar.gz" "$URL"

tar -xzf "$TMP/subs-check.tar.gz" -C "$TMP"

FOUND="$(find "$TMP" -type f -name 'subs-check*' ! -name '*.tar.gz' | head -1)"
[ -z "$FOUND" ] && { echo "❌ 压缩包中未找到 subs-check"; exit 1; }

mv "$FOUND" "$BIN" && chmod +x "$BIN"
echo "✅ 安装完成: $BIN ($("$BIN" --version 2>/dev/null || echo 'unknown'))"
