FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV RTSP_HWACCEL=vaapi

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python + GObject introspection bindings
    python3 \
    python3-pip \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    # GStreamer core
    gstreamer1.0-tools \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    # Plugin sets
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    # Intel iHD media driver
    intel-media-va-driver-non-free \
    libva-drm2 \
    libva2 \
    # rtmpsink dependency
    librtmp1 \
    # voaacenc dependency
    libvo-aacenc0 \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip3 install --break-system-packages -r /app/requirements.txt

COPY rtsp_switcher.py /app/rtsp_switcher.py
COPY settings.yaml.example /app/settings.yaml.example
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/bin/bash", "/run.sh"]
