#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv
fi

if [[ ! -x .venv/bin/python ]]; then
    if ! python3 -m venv .venv 2>/dev/null; then
        sudo apt-get update
        sudo apt-get install -y python3-venv
        python3 -m venv .venv
    fi
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
    read -r -p "Telegram api_id: " api_id
    while [[ ! "$api_id" =~ ^[0-9]+$ ]]; do
        read -r -p "api_id 必须是数字，请重新输入: " api_id
    done

    read -r -s -p "Telegram api_hash（输入不会显示）: " api_hash
    printf '\n'
    while [[ -z "$api_hash" ]]; do
        read -r -s -p "api_hash 不能为空，请重新输入: " api_hash
        printf '\n'
    done

    umask 077
    {
        printf 'TG_API_ID=%s\n' "$api_id"
        printf 'TG_API_HASH=%s\n' "$api_hash"
        printf 'TG_SESSION_NAME=data/tg_save_helper\n'
        printf 'OWNER_ID=\n'
        printf 'TG_DATABASE_PATH=data/tg_save_helper.sqlite3\n'
        printf 'LOG_LEVEL=INFO\n'
    } > .env
    unset api_hash
else
    echo "使用已有配置：$PROJECT_DIR/.env"
fi

chmod 600 .env
mkdir -p data
chmod 700 data

echo "开始 Telegram 首次登录。验证码请在手机 Telegram 中查看。"
.venv/bin/python -m src.main --login-only

service_user="$(id -un)"
unit_tmp="$(mktemp)"
trap 'rm -f "$unit_tmp"' EXIT
sed \
    -e "s|^User=.*|User=$service_user|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" \
    -e "s|^EnvironmentFile=.*|EnvironmentFile=$PROJECT_DIR/.env|" \
    -e "s|^ExecStart=.*|ExecStart=$PROJECT_DIR/.venv/bin/python -m src.main|" \
    tg-save-helper.service > "$unit_tmp"

sudo install -m 0644 "$unit_tmp" /etc/systemd/system/tg-save-helper.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-save-helper.service

echo
echo "安装完成，服务状态："
sudo systemctl --no-pager --full status tg-save-helper.service || true
echo
echo "现在用手机在 Telegram 收藏夹发送 /help 开始测试。"
