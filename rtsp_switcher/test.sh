

export GST_GL_PLATFORM=egl
export GST_GL_WINDOW=surfaceless
export GST_GL_API=opengl

gst-launch-1.0 -v glvideomixer name=mix ! \
video/x-raw\(memory:GLMemory\),width=1920,height=1080,framerate=30/1,format=NV12 ! \
nvh264enc name=enc bitrate=10000 gop-size=30 preset=p4 repeat-sequence-header=true rc-mode=vbr ! \
"video/x-h264,profile=high" ! \
h264parse ! \
flvmux streamable=true name=mux ! \
rtmpsink location=rtmp://192.168.1.10:1935/youtube \
gltestsrc pattern=smpte is-live=true ! "video/x-raw\(memory:GLMemory\),width=1920,height=1080,framerate=30/1" ! mix. \
gltestsrc pattern=snow is-live=true  ! "video/x-raw\(memory:GLMemory\),width=1920,height=1080,framerate=30/1" ! mix.


