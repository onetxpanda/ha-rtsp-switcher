#!/usr/bin/with-contenv bashio

export LIBVA_DRIVER_NAME=iHD
export LIBVA_DRM_DEVICE=/dev/dri/renderD128
export GST_VAAPI_DRM_DEVICE=/dev/dri/renderD128
export GST_GL_PLATFORM=egl
export GST_GL_WINDOW=surfaceless
export GST_GL_API=opengl

CONFIG_DIR=/config/rtsp_switcher
SETTINGS_FILE=${CONFIG_DIR}/settings.yaml

mkdir -p "${CONFIG_DIR}"

if [ ! -f "${SETTINGS_FILE}" ]; then
    bashio::log.warning "No settings.yaml found. Creating one from the example at ${SETTINGS_FILE} — edit it before starting the addon."
    cp /app/settings.yaml.example "${SETTINGS_FILE}"
fi

exec python3 /app/rtsp_switcher.py
