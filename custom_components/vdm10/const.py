"""Constants for the VDM10 intercom integration."""

from homeassistant.const import Platform

DOMAIN = "vdm10"

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CAMERA, Platform.EVENT]

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
CONF_ADVANCED = "advanced"  # collapsed section: ports/serial overrides
CONF_PRELOAD_STREAM = "preload_stream"  # keep the door stream hot in go2rtc (fast start, idle cost)
CONF_PROVIDER = "provider"  # collapsed section: optional SIP trunk (Linphone/PBX)
CONF_FRITZ = "fritz"  # collapsed section: optional Fritz trunk

CONF_SIP_HOST = "sip_host"
CONF_SIP_PORT = "sip_port"
CONF_SIP_USER = "sip_user"
CONF_SIP_PASS = "sip_pass"
CONF_SIP_TARGET = "sip_target"
CONF_SIP_DOMAIN = "sip_domain"
CONF_SIP_SRTP = "sip_srtp"
CONF_INBOUND_TARGET = "inbound_target"  # intercom extension rung when the SIP side calls us
CONF_FRITZ_HOST = "fritz_host"
CONF_FRITZ_PORT = "fritz_port"
CONF_FRITZ_DOMAIN = "fritz_domain"
CONF_FRITZ_USER = "fritz_user"
CONF_FRITZ_PASS = "fritz_pass"
CONF_FRITZ_TARGET = "fritz_target"

DISCOVER_MANUAL = "__manual__"  # select option: type values by hand

DEFAULT_REGISTRAR_PORT = 5065
DEFAULT_LISTEN_PORT = 5060
DEFAULT_RTSP_PORT = 554
DEFAULT_DOOR_USER = "admin"
DEFAULT_SIP_PORT = 5060
DEFAULT_FRITZ_PORT = 5060
DEFAULT_FRITZ_DOMAIN = "fritz.box"

DOOR_STREAM_PATH = "/ch1/main/av_stream"  # validated: metzler/media/rtsp_puller.py:27
DOOR_RTSP_PORT = 554
DOOR_ISAPI_PORT = 80

EVENT_RING = f"{DOMAIN}_ring"
EVENT_ANSWERED = f"{DOMAIN}_answered"
EVENT_CALL_ENDED = f"{DOMAIN}_call_ended"

SERVICE_ANSWER_CALL = "answer_call"
SERVICE_HANGUP_CALL = "hangup_call"

SIGNAL_VDM10_UPDATED = f"{DOMAIN}_updated"
