#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="3.3.1"
ZIP_PATH="${1:-/home/ubuntu/xingxiaodao-agent-v3.3.1.zip}"
APP_DIR="/opt/xingxiaodao"
STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE_DIR="/opt/xingxiaodao-v${VERSION}-stage-${STAMP}"
CURRENT_VERSION="$(grep -m1 '^version' "${APP_DIR}/pyproject.toml" 2>/dev/null | cut -d'"' -f2 || echo unknown)"
BACKUP_DIR="/opt/xingxiaodao-v${CURRENT_VERSION}-backup-${STAMP}"
FAILED_DIR="/opt/xingxiaodao-failed-${STAMP}"

restore_previous() {
  status=$?
  echo "升级失败（退出码：${status}），正在恢复上一版本…" >&2
  if [[ -d "${BACKUP_DIR}" ]]; then
    sudo systemctl stop xingxiaodao || true
    [[ -d "${APP_DIR}" ]] && sudo mv "${APP_DIR}" "${FAILED_DIR}" || true
    sudo mv "${BACKUP_DIR}" "${APP_DIR}"
  else
    sudo rm -rf "${STAGE_DIR}" || true
  fi
  sudo systemctl start xingxiaodao || true
  exit "${status}"
}
trap restore_previous ERR

upsert_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "${STAGE_DIR}/.env"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${STAGE_DIR}/.env"
  else
    printf '\n%s=%s\n' "${key}" "${value}" >>"${STAGE_DIR}/.env"
  fi
}

[[ -f "${ZIP_PATH}" ]] || { echo "未找到部署包：${ZIP_PATH}" >&2; exit 2; }
[[ -d "${APP_DIR}" && -f "${APP_DIR}/.env" ]] || { echo "未找到现有服务目录或生产 .env。" >&2; exit 2; }

echo "[1/6] 正在解压 v${VERSION} 部署包…"
sudo rm -rf "${STAGE_DIR}"; sudo mkdir -p "${STAGE_DIR}"
sudo unzip -q -o "${ZIP_PATH}" -d "${STAGE_DIR}"
if [[ ! -f "${STAGE_DIR}/pyproject.toml" ]]; then
  candidate="$(find "${STAGE_DIR}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
  [[ -n "${candidate}" && -f "${candidate}/pyproject.toml" ]] || { echo "部署包中未找到 pyproject.toml。" >&2; exit 2; }
  sudo sh -c "cp -a '${candidate}/.' '${STAGE_DIR}/'"
  sudo rm -rf "${candidate}"
fi

echo "[2/6] 正在继承现有生产环境配置…"
sudo cp "${APP_DIR}/.env" "${STAGE_DIR}/.env"
sudo chown -R ubuntu:ubuntu "${STAGE_DIR}"; sudo chmod 600 "${STAGE_DIR}/.env"
upsert_env MAX_UPLOAD_MB "${MAX_UPLOAD_MB:-100}"
upsert_env STEPFUN_ASR_POLL_TIMEOUT_SECONDS "${STEPFUN_ASR_POLL_TIMEOUT_SECONDS:-300}"

echo "[3/6] 正在切换服务目录…"
sudo systemctl stop xingxiaodao
sudo mv "${APP_DIR}" "${BACKUP_DIR}"
sudo mv "${STAGE_DIR}" "${APP_DIR}"

echo "[4/6] 正在正式目录中创建环境并安装依赖…"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
"${APP_DIR}/.venv/bin/pip" install -e "${APP_DIR}"

echo "[5/6] 正在启动服务…"
sudo systemctl start xingxiaodao

echo "[6/6] 正在等待本机健康检查…"
for _ in $(seq 1 30); do
  curl -fsS --max-time 3 http://127.0.0.1:8000/api/health >/tmp/xingxiaodao-health.json && break
  sleep 1
done
grep -q '"version":"3.3.1"' /tmp/xingxiaodao-health.json

echo "正在进行公网 HTTPS 健康检查…"
curl -fsS --max-time 15 https://62.234.95.211/api/health
echo; echo "v${VERSION} 部署成功。原版本备份目录：${BACKUP_DIR}"
