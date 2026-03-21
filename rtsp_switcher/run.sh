#!/bin/bash

export GST_GL_PLATFORM=egl
export GST_GL_WINDOW=surfaceless
export GST_GL_API=opengl

echo "=== vainfo diagnostic ==="
vainfo 2>&1 || true
echo "========================="

bash /usr/local/bin/hw-info 2>&1 || true

CONFIG_DIR=/config/rtsp_switcher
SETTINGS_FILE=${CONFIG_DIR}/settings.yaml

mkdir -p "${CONFIG_DIR}"

if [ ! -f "${SETTINGS_FILE}" ]; then
    echo "WARNING: No settings.yaml found. Creating one from the example at ${SETTINGS_FILE} — edit it before starting the addon."
    cp /app/settings.yaml.example "${SETTINGS_FILE}"
fi

exec python3 /app/rtsp_switcher.py
