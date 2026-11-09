"""Constants for the VDM10 intercom integration."""

from homeassistant.const import Platform

DOMAIN = "vdm10"

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CAMERA, Platform.SENSOR]

CONF_REGISTRAR_HOST = "registrar_host"
CONF_REGISTRAR_PORT = "registrar_port"
CONF_USERNAME = "username"  # SIP extension number, e.g. 10000000005
CONF_PASSWORD = "password"  # extension SIP password
CONF_SERIAL = "serial"  # regDevSerial
CONF_LOCAL_IP = "local_ip"  # this host's IP (bound + advertised in SIP/SDP)
CONF_LISTEN_PORT = "listen_port"  # local SIP UDP port
CONF_RTSP_PORT = "rtsp_port"  # local RTSP talkback server port (stations pull our mic here)
CONF_DOOR_HOST = "door_host"  # outdoor station IP (camera stream)
CONF_DOOR_USER = "door_user"  # door ISAPI/RTSP login (Basic auth)
CONF_DOOR_PASSWORD = "door_password"

DEFAULT_REGISTRAR_PORT = 5065
DEFAULT_LISTEN_PORT = 5060
DEFAULT_RTSP_PORT = 554
DEFAULT_DOOR_USER = "admin"

DOOR_STREAM_PATH = "/ch1/main/av_stream"  # validated: metzler/media/rtsp_puller.py:27
DOOR_RTSP_PORT = 554
DOOR_ISAPI_PORT = 80

EVENT_RING = f"{DOMAIN}_ring"
EVENT_ANSWERED = f"{DOMAIN}_answered"
EVENT_CALL_ENDED = f"{DOMAIN}_call_ended"

SERVICE_ANSWER_CALL = "answer_call"
SERVICE_HANGUP_CALL = "hangup_call"

SIGNAL_VDM10_UPDATED = f"{DOMAIN}_updated"
