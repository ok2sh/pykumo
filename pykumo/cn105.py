"""Helpers for building and decoding raw CN105/ITP serial frames.

These frames are transmitted onto the indoor unit's CN105 bus via the Kumo
adapter's ``indoorUnit.settings.rawITPFrame`` node (see the raw transport
methods on :class:`pykumo.py_kumo.PyKumo`). The wire format is::

    FC | type | 01 30 | payloadLen | payload | checksum

The checksum is ``(0xFC - sum(preceding_bytes)) & 0xFF``.
"""

PACKET_HEADER = 0xFC
# Standard CN105 sub-header that follows the type byte on every frame.
PACKET_SUBHEADER = bytes([0x01, 0x30])
# Info request/response type bytes.
INFO_REQUEST_TYPE = 0x42
INFO_RESPONSE_TYPE = 0x62
# Info code for the room/outdoor temperature response.
INFO_CODE_ROOM_TEMP = 0x03
# Info code for the operating/compressor status response.
INFO_CODE_STATUS = 0x06
# Info code for the sub mode / standby response.
INFO_CODE_STANDBY = 0x09
# Info request payloads are always padded to 16 bytes on the wire.
PAYLOAD_SIZE = 16
# A signed byte is used for the frame length in firmware, so 1..127 is usable.
MAX_FRAME_LEN = 127

# Sub mode map for the 0x09 response, raw byte index 8.
SUB_MODE = {
    0x00: "NORMAL",
    0x01: "WARMUP",
    0x02: "DEFROST",
    0x04: "PREHEAT",
    0x08: "STANDBY",
    0x10: "OFF",
}
# Indoor fan stage map for the 0x09 response, raw byte index 9.
STAGE = {
    0x00: "IDLE",
    0x01: "LOW",
    0x02: "GENTLE",
    0x03: "MEDIUM",
    0x04: "MODERATE",
    0x05: "HIGH",
    0x06: "DIFFUSE",
}
# Auto sub mode map for the 0x09 response, raw byte index 10. Two protocol
# generations share this byte: older 4-state units (0x00..0x03) and newer MFZ
# bitfield units (0x40/0x41/0x43).
AUTO_SUB_MODE = {
    0x00: "AUTO_OFF",
    0x01: "AUTO_COOL",
    0x02: "AUTO_HEAT",
    0x03: "AUTO_LEADER",
    0x40: "AUTO_INACTIVE",
    0x41: "AUTO_IDLE",
    0x43: "AUTO_ACTIVE",
}


def cn105_checksum(data: bytes) -> int:
    """Return the CN105 frame checksum for ``data`` (all preceding bytes)."""
    return (0xFC - sum(data)) & 0xFF


def build_cn105_frame(type_byte: int, payload: bytes) -> bytes:
    """Build a complete CN105 frame: header + payload + trailing checksum.

    ``payload`` is right-padded with zeros to :data:`PAYLOAD_SIZE` bytes, which
    matches how info requests appear on the wire.
    """
    if not 0 <= type_byte <= 0xFF:
        raise ValueError("type_byte must be 0..255")
    payload = bytes(payload)
    if len(payload) > PAYLOAD_SIZE:
        raise ValueError(f"payload must be <= {PAYLOAD_SIZE} bytes")
    payload = payload.ljust(PAYLOAD_SIZE, b"\x00")
    body = bytes([PACKET_HEADER, type_byte]) + PACKET_SUBHEADER
    body += bytes([len(payload)]) + payload
    return body + bytes([cn105_checksum(body)])


def build_info_request(code: int) -> bytes:
    """Build an info-request frame (type ``0x42``) for the given info ``code``."""
    if not 0 <= code <= 0xFF:
        raise ValueError("code must be 0..255")
    return build_cn105_frame(INFO_REQUEST_TYPE, bytes([code]))


def valid_cn105_reply(frame: bytes) -> bool:
    """True if ``frame`` is a well-formed CN105 frame with a valid checksum."""
    if not frame or len(frame) < 6:
        return False
    if frame[0] != PACKET_HEADER:
        return False
    return cn105_checksum(frame[:-1]) == frame[-1]


def decode_outdoor_temperature(frame: bytes):
    """Decode outdoor air temperature (C) from a ``0x03`` response frame.

    Uses raw byte index 10: ``(byte - 128) / 2`` when ``byte > 1``. Returns
    ``None`` when the byte is <= 1 (many outdoor units report this as
    'unavailable' while the compressor is idle) or the frame is too short.
    """
    if not frame or len(frame) < 11:
        return None
    byte = frame[10]
    if byte <= 1:
        return None
    return (byte - 128) / 2


def decode_room_temperature(frame: bytes):
    """Decode room temperature (C) from a ``0x03`` response frame.

    Prefers encoding B at raw byte index 11 (``(byte - 128) / 2`` when
    nonzero); falls back to the legacy map at raw byte index 8
    (``0x00..0x1F`` -> ``10..41 C``). Returns ``None`` if neither is usable.
    """
    if not frame or len(frame) < 12:
        return None
    encoding_b = frame[11]
    if encoding_b:
        return (encoding_b - 128) / 2
    legacy = frame[8]
    if legacy <= 0x1F:
        return 10 + legacy
    return None


def _valid_reply_for_code(frame, code: int, min_len: int) -> bool:
    """True if ``frame`` is a valid reply for info ``code`` and long enough.

    Checks the frame is well-formed (:func:`valid_cn105_reply`), that the
    echoed info code at raw byte index 5 matches ``code``, and that the frame
    has at least ``min_len`` bytes so the target byte can be read.
    """
    if not valid_cn105_reply(frame) or len(frame) < min_len:
        return False
    return frame[5] == code


def decode_operating(frame):
    """Decode the operating flag from a ``0x06`` status response frame.

    Uses raw byte index 9: ``1`` = compressor running, ``0`` = standby.
    Returns ``True``/``False`` accordingly, or ``None`` if the frame is not a
    valid ``0x06`` reply.
    """
    if not _valid_reply_for_code(frame, INFO_CODE_STATUS, 10):
        return None
    return frame[9] == 1


def decode_compressor_frequency(frame):
    """Decode the compressor frequency (Hz) from a ``0x06`` status response.

    Uses raw byte index 8, but forces the result to ``0`` when the operating
    byte (raw index 9) is ``0``, because some units report noise on this byte
    while idle. Returns ``None`` if the frame is not a valid ``0x06`` reply.
    """
    if not _valid_reply_for_code(frame, INFO_CODE_STATUS, 10):
        return None
    if frame[9] == 0:
        return 0
    return frame[8]


def decode_sub_mode(frame):
    """Decode the sub mode from a ``0x09`` standby response frame.

    Maps raw byte index 8 via :data:`SUB_MODE`. Unlike ESPHome (which keeps the
    previous value on an unrecognized byte), pykumo reads on-demand and is
    stateless, so an unmapped byte returns ``None``. Also returns ``None`` if
    the frame is not a valid ``0x09`` reply.
    """
    if not _valid_reply_for_code(frame, INFO_CODE_STANDBY, 9):
        return None
    return SUB_MODE.get(frame[8])


def decode_stage(frame):
    """Decode the indoor fan stage from a ``0x09`` standby response frame.

    Maps raw byte index 9 via :data:`STAGE`. Unlike ESPHome (which keeps the
    previous value on an unrecognized byte), pykumo is stateless, so an unmapped
    byte returns ``None``. Also returns ``None`` if the frame is not a valid
    ``0x09`` reply.
    """
    if not _valid_reply_for_code(frame, INFO_CODE_STANDBY, 10):
        return None
    return STAGE.get(frame[9])


def decode_auto_sub_mode(frame):
    """Decode the auto sub mode from a ``0x09`` standby response frame.

    Maps raw byte index 10 via :data:`AUTO_SUB_MODE`. Unlike ESPHome (which
    keeps the previous value on an unrecognized byte), pykumo is stateless, so
    an unmapped byte returns ``None``. Also returns ``None`` if the frame is not
    a valid ``0x09`` reply.
    """
    if not _valid_reply_for_code(frame, INFO_CODE_STANDBY, 11):
        return None
    return AUTO_SUB_MODE.get(frame[10])


def decode_compressor_runtime_minutes(frame):
    """Decode the compressor runtime counter (minutes) from a ``0x03`` response.

    24-bit big-endian value at raw byte indices 16, 17, 18. This counter
    advances by ~1 per minute only while the compressor actually runs (it stays
    flat while the unit is off or idle), so it is a compressor-runtime counter,
    not a power-on counter. Returns ``None`` if the frame is not a valid ``0x03``
    reply long enough to reach the counter.
    """
    if not _valid_reply_for_code(frame, INFO_CODE_ROOM_TEMP, 19):
        return None
    return (frame[16] << 16) | (frame[17] << 8) | frame[18]


def decode_compressor_runtime_hours(frame):
    """Decode the compressor runtime counter (hours) from a ``0x03`` response.

    Convenience wrapper over :func:`decode_compressor_runtime_minutes` that
    divides by 60. Returns ``None`` if the frame is not a valid ``0x03`` reply.
    """
    minutes = decode_compressor_runtime_minutes(frame)
    if minutes is None:
        return None
    return minutes / 60.0


def decode_status_0x03(frame):
    """Decode the full ``0x03`` room/outdoor/runtime status into a dict.

    Returns ``{"room_temperature", "outdoor_temperature",
    "compressor_runtime_minutes", "compressor_runtime_hours"}``, or ``None`` if
    the frame is not a valid ``0x03`` reply. Individual values may still be
    ``None`` (e.g. outdoor temperature while the compressor is idle).
    """
    if not _valid_reply_for_code(frame, INFO_CODE_ROOM_TEMP, 19):
        return None
    return {
        "room_temperature": decode_room_temperature(frame),
        "outdoor_temperature": decode_outdoor_temperature(frame),
        "compressor_runtime_minutes": decode_compressor_runtime_minutes(frame),
        "compressor_runtime_hours": decode_compressor_runtime_hours(frame),
    }


def decode_status_0x06(frame):
    """Decode the full ``0x06`` status into a dict.

    Returns ``{"operating", "compressor_frequency"}``, or ``None`` if the frame
    is not a valid ``0x06`` reply.
    """
    if not _valid_reply_for_code(frame, INFO_CODE_STATUS, 10):
        return None
    return {
        "operating": decode_operating(frame),
        "compressor_frequency": decode_compressor_frequency(frame),
    }


def decode_status_0x09(frame):
    """Decode the full ``0x09`` standby status into a dict.

    Returns ``{"sub_mode", "stage", "auto_sub_mode"}``, or ``None`` if the frame
    is not a valid ``0x09`` reply. Individual values may still be ``None`` on an
    unrecognized byte.
    """
    if not _valid_reply_for_code(frame, INFO_CODE_STANDBY, 11):
        return None
    return {
        "sub_mode": decode_sub_mode(frame),
        "stage": decode_stage(frame),
        "auto_sub_mode": decode_auto_sub_mode(frame),
    }
