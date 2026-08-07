#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_PATH="${ESPELHO_ZAP_CONFIG:-${HOME}/.config/espelho-zap/config.toml}"
exec espelho-zap --config "${CONFIG_PATH}" doctor "$@"
