#!/usr/bin/with-contenv bashio

export LIBVA_DRIVER_NAME=iHD
export LIBVA_DRM_DEVICE=/dev/dri/renderD128
export GST_VAAPI_DRM_DEVICE=/dev/dri/renderD128
export GST_GL_PLATFORM=egl
export GST_GL_WINDOW=surfaceless
export GST_GL_API=opengl

exec python3 /app/rtsp_switcher.py
