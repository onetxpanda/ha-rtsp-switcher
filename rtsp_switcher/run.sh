#!/bin/bash

export GST_GL_PLATFORM=egl
export GST_GL_WINDOW=surfaceless
export GST_GL_API=opengl

echo "=== vainfo diagnostic (DRM) ==="
vainfo --display drm --device-paths /dev/dri/renderD128 2>&1 || true
echo "================================"

echo "=== GStreamer va plugin ==="
gst-inspect-1.0 va 2>&1 || true
echo "==========================="

echo "=== GST_DEBUG va probe ==="
GST_DEBUG=va*:5 gst-inspect-1.0 vah264dec 2>&1 | tail -60 || true
echo "=========================="

bash /usr/local/bin/hw-info 2>&1 || true

CONFIG_DIR=/config/rtsp_switcher
SETTINGS_FILE=${CONFIG_DIR}/settings.yaml

mkdir -p "${CONFIG_DIR}"

if [ ! -f "${SETTINGS_FILE}" ]; then
    echo "WARNING: No settings.yaml found. Creating one from the example at ${SETTINGS_FILE} — edit it before starting the addon."
    cp /app/settings.yaml.example "${SETTINGS_FILE}"
fi

exec python3 /app/rtsp_switcher.py
