# Home Assistant addon image
# Uses the official HA base (Alpine), which provides bashio, with-contenv, and s6-overlay.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM ${BUILD_FROM}

# Enable community repo for gst-plugins-bad/ugly/libav and VA-API packages
RUN echo "https://dl-cdn.alpinelinux.org/alpine/v3.20/community" >> /etc/apk/repositories \
  && apk update && apk add --no-cache \
    # Python + GObject introspection bindings
    python3 \
    py3-pip \
    py3-gobject3 \
    # GStreamer core + plugins
    gstreamer \
    gst-plugins-base \
    gst-plugins-good \
    gst-plugins-bad \
    gst-plugins-ugly \
    gst-libav \
    # VA-API plugin + Intel iHD driver
    gst-vaapi \
    libva \
    libva-intel-driver \
    intel-media-driver \
    # rtmpsink / voaacenc runtime libs
    librtmp \
    vo-aacenc

COPY requirements.txt /app/requirements.txt
RUN pip3 install --break-system-packages -r /app/requirements.txt

COPY rtsp_switcher.py /app/rtsp_switcher.py
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
