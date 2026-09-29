#!/usr/bin/env bash
# 在云服务器上：把新 Joy.exe 发布到下载站并刷新 latest.json
# 用法：
#   ./publish.sh /path/to/Joy.exe 0.1.1 "修复说明写这里"
set -euo pipefail

EXE="${1:?用法: $0 Joy.exe 版本号 [更新说明]}"
VER="${2:?缺少版本号，例如 0.1.1}"
NOTES="${3:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REL_DIR="$ROOT/releases"
API="$ROOT/api/latest.json"
NAME="Joy-${VER}.exe"
DEST="$REL_DIR/$NAME"

mkdir -p "$REL_DIR" "$ROOT/api"
cp -f "$EXE" "$DEST"
SHA="$(sha256sum "$DEST" | awk '{print $1}')"
SIZE_MB="$(du -m "$DEST" | awk '{print $1}')"

# changelog JSON 转义
NOTES_ESC="$(printf '%s' "$NOTES" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"

cat > "$API" <<EOF
{
  "version": "${VER}",
  "min_version": "0.1.0",
  "url": "/releases/${NAME}",
  "sha256": "${SHA}",
  "size_mb": ${SIZE_MB},
  "force": false,
  "changelog": ${NOTES_ESC}
}
EOF

echo "已发布: $DEST"
echo "sha256: $SHA"
echo "清单: $API"
echo "请确认 Nginx root 指向: $ROOT"
