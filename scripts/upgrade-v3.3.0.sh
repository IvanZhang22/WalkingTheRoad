#!/usr/bin/env bash
# 行小道 v3.3.0：腾讯云服务器安全升级脚本
set -Eeuo pipefail

VERSION="3.3.0"
ZIP_PATH="${1:-/home/ubuntu/xingxiaodao-v3.3.0-methodology-knowledge-base.zip}"
APP_DIR="/opt/xingxiaodao"
STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE_DIR="/opt/xingxiaodao-v${VERSION}-stage-${STAMP}"
BACKUP_DIR="/opt/xingxiaodao-v$(grep -m1 '^version' "${APP_DIR}/pyproject.toml" 2>/dev/null | cut -d'"' -f2 || echo unknown)-backup-${STAMP}"
FAILED_DIR="/opt/xingxiaodao-failed-${STAMP}"

restore_previous() {
  exit_code=$?
  echo "升级失败，正在恢复旧版本（退出码：${exit_code}）…" >&2
  sudo systemctl stop xingxiaodao || true
  if [[ -d "${APP_DIR}" ]]; then
    sudo mv "${APP_DIR}" "${FAILED_DIR}" || true
  fi
  if [[ -d "${BACKUP_DIR}" ]]; then
    sudo mv "${BACKUP_DIR}" "${APP_DIR}"
  fi
  sudo systemctl start xingxiaodao || true
  echo "已恢复旧版本；失败目录：${FAILED_DIR}" >&2
  exit "${exit_code}"
}
trap restore_previous ERR

[[ -f "${ZIP_PATH}" ]] || { echo "找不到部署包：${ZIP_PATH}" >&2; exit 2; }
[[ -d "${APP_DIR}" ]] || { echo "找不到现有服务目录：${APP_DIR}" >&2; exit 2; }

echo "[1/6] 解压 v${VERSION} 部署包…"
sudo rm -rf "${STAGE_DIR}"
sudo mkdir -p "${STAGE_DIR}"
sudo unzip -q -o "${ZIP_PATH}" -d "${STAGE_DIR}"

if [[ ! -f "${STAGE_DIR}/pyproject.toml" ]]; then
  candidate="$(find "${STAGE_DIR}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
  [[ -n "${candidate}" && -f "${candidate}/pyproject.toml" ]] || {
    echo "部署包中未找到 pyproject.toml。" >&2
    exit 2
  }
  sudo sh -c "cp -a '${candidate}/.' '${STAGE_DIR}/'"
  sudo rm -rf "${candidate}"
fi

echo "[2/6] 继承生产环境变量…"
sudo cp "${APP_DIR}/.env" "${STAGE_DIR}/.env"
sudo chown -R ubuntu:ubuntu "${STAGE_DIR}"
sudo chmod 600 "${STAGE_DIR}/.env"

echo "[3/6] 创建虚拟环境并安装依赖…"
python3 -m venv "${STAGE_DIR}/.venv"
"${STAGE_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
"${STAGE_DIR}/.venv/bin/pip" install -e "${STAGE_DIR}"

echo "[4/6] 切换服务目录…"
sudo systemctl stop xingxiaodao
sudo mv "${APP_DIR}" "${BACKUP_DIR}"
sudo mv "${STAGE_DIR}" "${APP_DIR}"
sudo systemctl start xingxiaodao

echo "[5/6] 等待健康检查…"
for _ in $(seq 1 20); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/api/health >/tmp/xingxiaodao-health.json; then
    break
  fi
  sleep 1
done
grep -q '"version":"3.3.0"' /tmp/xingxiaodao-health.json

echo "[6/6] 公网健康检查…"
curl -fsS --max-time 10 https://62.234.95.211/api/health
echo
echo "v${VERSION} 部署成功。"
echo "原版本备份目录：${BACKUP_DIR}"
