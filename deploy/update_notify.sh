#!/usr/bin/env bash
set -euo pipefail

APP_NAME="notify-app"
APP_ROOT="/opt/${APP_NAME}"
APP_DIR="${APP_ROOT}/current"
VENV_DIR="${APP_ROOT}/venv"
REPO_URL="https://github.com/RazaS/notify.git"
BRANCH="${NOTIFY_DEPLOY_BRANCH:-main}"

mkdir -p "${APP_ROOT}"

if [ ! -d "${APP_DIR}/.git" ]; then
    rm -rf "${APP_DIR}"
    git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${APP_DIR}"
else
    git -C "${APP_DIR}" fetch origin "${BRANCH}"
    git -C "${APP_DIR}" checkout "${BRANCH}"
    git -C "${APP_DIR}" reset --hard "origin/${BRANCH}"
fi

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt"

install -d -m 755 "${APP_ROOT}/data"

systemctl daemon-reload
systemctl restart "${APP_NAME}.service"
