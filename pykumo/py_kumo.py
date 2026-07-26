"""Class used to represent indoor units"""

import datetime
import logging
import time
from collections.abc import MutableMapping

from .schedule import UnitSchedule

from .const import CACHE_INTERVAL_SECONDS, POSSIBLE_SENSORS, SETTABLE_TEMP_SOURCES
from .cn105 import (
    INFO_CODE_ROOM_TEMP,
    INFO_CODE_STANDBY,
    INFO_CODE_STATUS,
    INFO_RESPONSE_TYPE,
    MAX_FRAME_LEN,
    build_info_request,
    decode_auto_sub_mode,
    decode_compressor_frequency,
    decode_compressor_runtime_hours,
    decode_compressor_runtime_minutes,
    decode_operating,
    decode_outdoor_temperature,
    decode_room_temperature,
    decode_stage,
    decode_status_0x03,
    decode_status_0x06,
    decode_status_0x09,
    decode_sub_mode,
    valid_cn105_reply,
)
from .py_kumo_base import PyKumoBase

_LOGGER = logging.getLogger(__name__)
ALL_FAN_SPEEDS = ["superQuiet", "quiet", "low", "Low", "powerful", "superPowerful"]
# Poll timing for reading back a raw CN105 reply after transmitting a frame.
# The adapter's readback buffer is volatile, so poll it repeatedly. Different
# info codes answer at very different speeds off the same unit (0x03 ~2.7s,
# 0x09 ~11s), and some codes (e.g. 0x06 on units that do not implement it) never
# answer, so the default window is generous and a single miss just yields None.
RAW_REPLY_POLL_INTERVAL_SECONDS = 0.5
RAW_REPLY_TIMEOUT_SECONDS = 20.0


def merge(d, v):
    """
    Merge two dictionaries.

    Merge dict-like `v` into dict-like `d`. In case keys between them are the same, merge
    their sub-dictionaries where possible. Otherwise, values in `v` overwrite `d`.
    """
    for key in v:
        if (
            key in d
            and isinstance(d[key], MutableMapping)
            and isinstance(v[key], MutableMapping)
        ):
            d[key] = merge(d[key], v[key])
        else:
            d[key] = v[key]
    return d


class PyKumo(PyKumoBase):
    """Talk to and control one indoor unit."""

    # pylint: disable=R0904, R0902

    def __init__(
        self,
        name,
        addr,
        cfg_json,
        timeouts=None,
        serial=None,
        use_schedule: bool = False,
    ):
        """Constructor"""
        self._last_reboot = None
        self._unit_schedule = UnitSchedule(self) if use_schedule else None
        super().__init__(name, addr, cfg_json, timeouts, serial)

    def _rebootable_response(self, response):
        """
        Check whether response warrants immediate reboot of adapter
        """
        return response.get(
            "_api_error", ""
        ) == "serializer_error" or "__no_memory" in str(response)

    def _retryable_response(self, response):
        """
        Check whether response is retryable
        """
        return (
            response.get("_api_error", "") == "serializer_error"
            or response.get("_api_error", "") == "device_authentication_error"
            or "__no_memory" in str(response)
        )

    def _retrieve_attributes(
        self,
        query_path: list[str],
        needed: list[str],
        do_top_query: bool = True,
        retries=3,
    ) -> dict:
        """Try to retrieve a base query, but in specific error conditions retrieve specific
        needed attributes individually.
        """
        base_query = '{"c":{'
        for item in query_path:
            base_query += '"' + item + '":{'
        base_query += "}" * (len(query_path) + 2)
        query = base_query.encode("utf-8")
        try:
            should_reboot = False
            response = None
            if do_top_query:
                tries = 0
                while tries < retries:
                    response = self._request(query)
                    if self._rebootable_response(response):
                        should_reboot = True
                        break
                    if self._retryable_response(response):
                        _LOGGER.info(f"Retry {tries} main query due to {response}")
                        time.sleep(1.0)
                        tries += 1
                    else:
                        break
            built_response = {"r": {}}
            if not should_reboot and (
                not response or self._retryable_response(response)
            ):
                # Use individual attribute queries
                for attribute in needed:
                    if should_reboot:
                        break
                    attr_query = base_query.replace(
                        "{}", '{"' + attribute + '":{}}'
                    ).encode("utf-8")
                    tries = 0
                    while tries < retries:
                        sub_response = self._request(attr_query)
                        if self._rebootable_response(sub_response):
                            should_reboot = True
                            break
                        if self._retryable_response(sub_response):
                            _LOGGER.info(
                                f"Retry {tries} sub query due to {sub_response}"
                            )
                            time.sleep(1.0)
                            tries += 1
                        else:
                            break

                    if attribute in str(sub_response):
                        built_response = merge(built_response, sub_response)
                    else:
                        _LOGGER.warning(
                            f"{self._name}: Did not get {attribute} from {attr_query}: "
                            f"{sub_response}"
                        )
            if built_response.get("r"):
                # Got at least some good sub-responses
                response = built_response
            now = datetime.datetime.now()
            if should_reboot and (
                not self._last_reboot
                or self._last_reboot < now - datetime.timedelta(minutes=30)
            ):
                # Attempt to reboot the adapter
                _LOGGER.warning(f"{self._name}: Attempting to reboot Kumo adapter")
                self._last_reboot = now
                self.do_reboot()
                time.sleep(5.0)
                return self._retrieve_attributes(
                    query_path, needed, do_top_query, retries
                )
        except Exception as e:
            _LOGGER.warning("Exception fetching %s: %s", base_query, str(e))
        return response

    def _compute_has_mode_auto(self, auto_mode_prevention: bool) -> bool:
        """True if the unit supports auto (heat/cool) mode.

        Honors the adapter's autoModePrevention flag, but falls back to the
        unit profile's auto setpoints, since some installer configurations
        set autoModePrevention=True even though the unit (and the Mitsubishi
        Comfort app) treat auto mode as supported. Checks both
        maximumSetPoints and minimumSetPoints for an 'auto' key.
        """
        if not auto_mode_prevention:
            return True
        max_sp = self._profile.get("maximumSetPoints", {}) or {}
        min_sp = self._profile.get("minimumSetPoints", {}) or {}
        return "auto" in max_sp or "auto" in min_sp

    def update_status(self):
        """Retrieve and cache current status dictionary if enough time
        has passed
        """

        # Use cycle-aware session management to optimize multiple requests during status update,
        # then close session at the end of the cycle to send a clean FIN to the adapter.
        self.begin_cycle()
        try:
            now = time.monotonic()
            if (
                now - self._last_status_update > CACHE_INTERVAL_SECONDS
                or "mode" not in self._status
            ):
                query = ["indoorUnit", "status"]
                needed = [
                    "mode",
                    "standby",
                    "spHeat",
                    "spCool",
                    "roomTemp",
                    "fanSpeed",
                    "vaneDir",
                    "filterDirty",
                    "defrost",
                    "tempSource",
                    "activeThermistor",
                ]
                # Following not currently used:
                # 'hotAdjust', 'runTest'
                response = self._retrieve_attributes(query, needed)
                raw_status = response
                try:
                    self._status = raw_status["r"]["indoorUnit"]["status"]
                    self._last_status_update = now
                except KeyError as ke:
                    _LOGGER.warning(
                        f"{self._name}: Error retrieving status from {response}: "
                        f"{str(ke)}"
                    )
                    return False

                self._sensors = []
                for s in range(POSSIBLE_SENSORS):
                    s_str = f"{s}"
                    query = ["sensors", s_str]
                    needed = [
                        "uuid",
                        "humidity",
                        "temperature",
                        "battery",
                        "rssi",
                        "txPower",
                    ]

                    response = self._retrieve_attributes(query, needed)

                    try:
                        sensor = response["r"]["sensors"][s_str]
                        if isinstance(sensor, dict) and sensor.get("uuid"):
                            self._sensors.append(sensor)
                        else:
                            # No sensor found at this index; skip the rest
                            break
                    except KeyError as ke:
                        _LOGGER.warning(
                            f"{self._name}: Error retrieving sensors from {response}: "
                            f"{str(ke)}"
                        )
                        return False

                query = ["indoorUnit", "profile"]
                needed = [
                    "numberOfFanSpeeds",
                    "hasFanSpeedAuto",
                    "hasVaneSwing",
                    "hasModeDry",
                    "hasModeHeat",
                    "hasModeVent",
                    "hasModeAuto",
                    "hasVaneDir",
                    "maximumSetPoints",
                    "minimumSetPoints",
                ]
                # Following not currently used
                # 'extendedTemps', 'usesSetPointInDryMode', 'hasHotAdjust', 'hasDefrost',
                # 'hasStandby'
                response = self._retrieve_attributes(query, needed)
                try:
                    self._profile = response["r"]["indoorUnit"]["profile"]
                except KeyError as ke:
                    _LOGGER.warning(
                        f"{self._name}: Error retrieving profile from {response}: "
                        f"{str(ke)}"
                    )
                    return False

                # Edit profile with settings from adapter
                query = ["adapter", "status"]
                needed = [
                    "autoModePrevention",
                    "userHasModeDry",
                    "userHasModeHeat",
                    "localNetwork",
                    "runState",
                ]
                # Following not currently used:
                # 'name', 'roomTempOffset', 'userMinCoolSetPoint', 'userMaxHeatSetPoint',
                # 'ledDisabled', 'serverHostname'
                # ['adapter', 'info'] not used:
                # ['macAddress', 'serialNumber', 'isTestMode', 'firmwareVersion']
                response = self._retrieve_attributes(query, needed)
                try:
                    status = response["r"]["adapter"]["status"]
                    self._profile["hasModeAuto"] = self._compute_has_mode_auto(
                        status.get("autoModePrevention", False)
                    )
                    if not status.get("userHasModeDry", False):
                        self._profile["hasModeDry"] = False
                    if not status.get("userHasModeHeat", False):
                        self._profile["hasModeHeat"] = False
                    try:
                        self._profile["wifiRSSI"] = status["localNetwork"][
                            "stationMode"
                        ]["RSSI"]
                    except KeyError:
                        self._profile["wifiRSSI"] = None
                    self._profile["runState"] = status.get("runState", "unknown")
                except KeyError as ke:
                    _LOGGER.warning(
                        f"{self._name}: Error retrieving adapter profile from {response}: "
                        f"{str(ke)}"
                    )
                    return False

                # Edit profile with data from MHK2 if present
                query = '{"c":{"mhk2":{"status":{}}}}'.encode("utf-8")
                response = self._request(query)
                try:
                    self._mhk2 = response["r"]["mhk2"]
                    if isinstance(self._mhk2, dict):
                        mhk2_humidity = self._mhk2["status"]["indoorHumid"]

                        if mhk2_humidity is not None:
                            # Add a sensor entry for the MHK2 unit.
                            mhk2_sensor_value = {
                                "battery": None,
                                "humidity": mhk2_humidity,
                                "rssi": None,
                                "temperature": None,
                                "txPower": None,
                                "uuid": None,
                            }
                            self._sensors.append(mhk2_sensor_value)
                except (KeyError, TypeError) as e:
                    # We don't bailout here since the MHK2 component is optional.
                    _LOGGER.info(
                        f"{self._name}: Error retrieving MHK2 status from {response}: {e}"
                    )
                    pass

            if self._unit_schedule is not None:
                self._unit_schedule.fetch()

            return True
        finally:
            self.end_cycle()

    def get_mode(self):
        """Last retrieved operating mode from unit"""
        try:
            val = self._status["mode"]
        except KeyError:
            val = None
        return val

    def get_standby(self):
        """Return if the unit is in standby"""
        try:
            val = self._status["standby"]
        except KeyError:
            val = None
        return val

    def get_heat_setpoint(self):
        """Last retrieved heat setpoint from unit"""
        try:
            val = self._status["spHeat"]
        except KeyError:
            val = None
        return val

    def get_cool_setpoint(self):
        """Last retrieved cooling setpoint from unit"""
        try:
            val = self._status["spCool"]
        except KeyError:
            val = None
        return val

    def get_current_temperature(self):
        """Last retrieved current temperature from unit"""
        try:
            val = self._status["roomTemp"]
        except KeyError:
            val = None
        return val

    def get_temp_source(self):
        """Last retrieved temperature source from unit"""
        try:
            val = self._status["tempSource"]
        except KeyError:
            val = None
        return val

    def get_active_thermistor(self):
        """Last retrieved active thermistor from unit. Reports 'api' while an
        injected room temperature is live.
        """
        try:
            val = self._status["activeThermistor"]
        except KeyError:
            val = None
        return val

    def get_fan_speeds(self):
        """List of valid fan speeds for unit"""
        try:
            speeds = self._profile["numberOfFanSpeeds"]
        except KeyError:
            speeds = 5
        if speeds not in (5, 4, 3):
            _LOGGER.info(
                "Unit reports a different number of fan speeds than "
                "supported, %d != [5|4|3]. Please report which ones work!",
                self._profile["numberOfFanSpeeds"],
            )

        if speeds == 3:
            # Intentionally return all 5 speeds even though the profile reports
            # numberOfFanSpeeds=3.  These units under-report their capability:
            # real hardware accepts the full superQuiet..superPowerful range,
            # as confirmed on units in the field.
            valid_speeds = ["superQuiet", "quiet", "low", "powerful", "superPowerful"]
        elif speeds == 4:
            valid_speeds = ["quiet", "Low", "powerful", "superPowerful"]
        else:
            valid_speeds = ["superQuiet", "quiet", "low", "powerful", "superPowerful"]
        try:
            if self._profile["hasFanSpeedAuto"]:
                valid_speeds.append("auto")
        except KeyError:
            pass
        return valid_speeds

    def get_vane_directions(self):
        """List of valid vane directions for unit"""
        if not self.has_vane_direction():
            _LOGGER.info("Unit does not support vane direction")
            return []

        valid_directions = [
            "horizontal",
            "midhorizontal",
            "midpoint",
            "midvertical",
            "vertical",
            "auto",
        ]
        try:
            if self._profile["hasVaneSwing"]:
                valid_directions.append("swing")
        except KeyError:
            pass
        return valid_directions

    def get_fan_speed(self):
        """Last retrieved fan speed mode from unit"""
        try:
            val = self._status["fanSpeed"]
        except KeyError:
            val = None
        return val

    def get_vane_direction(self):
        """Last retrieved vane direction mode from unit"""
        try:
            val = self._status["vaneDir"]
        except KeyError:
            val = None
        return val

    def get_current_humidity(self):
        """Last retrieved humidity from sensor or MHK2, if any"""
        val = None
        try:
            for sensor in self._sensors:
                if sensor["humidity"] is not None:
                    return sensor["humidity"]
        except KeyError:
            val = None
        return val

    def get_current_sensor_temperature(self):
        """Last retrieved temperature from sensor, if any"""
        val = None
        try:
            for sensor in self._sensors:
                if sensor["temperature"] is not None:
                    return sensor["temperature"]
        except KeyError:
            val = None
        return val

    def get_unit_schedule(self) -> UnitSchedule | None:
        """Retrieve the program (UnitSchedule) from sensor, if any."""
        return self._unit_schedule

    def get_sensor_battery(self):
        """Last retrieved battery percentage from sensor, if any"""
        val = None
        try:
            for sensor in self._sensors:
                if sensor["battery"] is not None:
                    return sensor["battery"]
        except KeyError:
            val = None
        return val

    def get_runstate(self):
        """Last retrieved runState, if any. True if unit has heat mode."""
        val = None
        try:
            val = self._profile["runState"]
        except KeyError:
            val = None
        return val

    def get_filter_dirty(self):
        """Last retrieved filter status from unit"""
        try:
            val = self._status["filterDirty"]
        except KeyError:
            val = None
        return val

    def get_defrost(self):
        """Last retrieved defrost status from unit"""
        try:
            val = self._status["defrost"]
        except KeyError:
            val = None
        return val

    def get_hold_time(self):
        """Get hold time from MHK2"""
        query = '{"c":{"mhk2":{"hold":{"adapter":{"endTime":{}}}}}}'.encode("utf-8")
        response = self._request(query)
        try:
            end_time = response["r"]["mhk2"]["hold"]["adapter"]["endTime"]
        except KeyError:
            end_time = None
        return end_time

    def get_hold_status(self):
        """Get hold status similar to representation on kumo app and MHK2 display"""
        end_time = self.get_hold_time()
        # mhk returns 3774499593 for "permanent hold"
        if end_time is None:
            _LOGGER.warning("End time not available")
            hold_status = ""
        elif end_time == 3774499593:
            hold_status = "permanent hold"
        elif end_time == 0:
            hold_status = "following schedule"
        elif (end_time - time.time()) > 82800:  # 23 hours
            days = round((end_time - time.time()) / 86400)
            hold_status = f"hold for {days} days"
        else:
            dt = datetime.datetime.fromtimestamp(end_time)
            hold_status = f"hold until {dt.strftime('%H:%M')}"
        return hold_status

    def has_dry_mode(self):
        """True if unit has dry (dehumidify) mode"""
        val = None
        try:
            val = self._profile["hasModeDry"]
        except KeyError:
            val = False
        return val

    def has_heat_mode(self):
        """True if unit has heat mode"""
        val = None
        try:
            val = self._profile["hasModeHeat"]
        except KeyError:
            val = False
        return val

    def has_vent_mode(self):
        """True if unit has vent (fan) mode"""
        val = None
        try:
            val = self._profile["hasModeVent"]
        except KeyError:
            val = False
        return val

    def has_auto_mode(self):
        """True if unit has auto (heat/cool) mode"""
        val = None
        try:
            val = self._profile["hasModeAuto"]
        except KeyError:
            val = False
        return val

    def has_vane_direction(self):
        """True if unit supports changing its vane direction (aka swing)"""
        val = None
        try:
            val = self._profile["hasVaneDir"]
        except KeyError:
            val = False
        return val

    def set_mode(self, mode):
        """Change operation mode. Valid modes: off, cool sometimes also heat,
        dry, vent, auto
        """
        modes = ["off", "cool"]
        if self.has_dry_mode():
            modes.append("dry")
        if self.has_heat_mode():
            modes.append("heat")
        if self.has_vent_mode():
            modes.append("vent")
        if self.has_auto_mode():
            modes.append("auto")
        if mode not in modes:
            _LOGGER.warning("Attempting to set invalid mode %s", mode)
            return {}

        command = ('{"c":{"indoorUnit":{"status":{"mode":"%s"}}}}' % mode).encode(
            "utf-8"
        )
        response = self._request(command)
        self._status["mode"] = mode
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response

    def set_heat_setpoint(self, setpoint):
        """Change setpoint for heat (in degrees C)"""
        # TODO: honor min/max from profile
        setpoint = round(float(setpoint), 1)
        command = (
            '{"c": { "indoorUnit": { "status": { "spHeat": %f } } } }' % setpoint
        ).encode("utf-8")
        response = self._request(command)
        self._status["spHeat"] = setpoint
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response

    def set_cool_setpoint(self, setpoint):
        """Change setpoint for cooling (in degrees C)"""
        # TODO: honor min/max from profile
        setpoint = round(float(setpoint), 2)
        command = (
            '{"c": { "indoorUnit": { "status": { "spCool": %f } } } }' % setpoint
        ).encode("utf-8")
        response = self._request(command)
        self._status["spCool"] = setpoint
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response

    def set_fan_speed(self, speed):
        """Change fan speed. Valid speeds: superQuiet, quiet, low, powerful,
        superPowerful, sometimes auto
        """
        if speed not in ALL_FAN_SPEEDS + ["auto"]:
            _LOGGER.warning("Attempting to set invalid fan speed %s", speed)
            return {}
        valid_speeds = self.get_fan_speeds()
        if speed not in valid_speeds:
            _LOGGER.warning(
                "Unit does not report fan speed %s as supported. Setting anyway", speed
            )
        command = (
            '{"c": { "indoorUnit": { "status": { "fanSpeed": "%s" } } } }' % speed
        ).encode("utf-8")
        response = self._request(command)
        self._status["fanSpeed"] = speed
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response

    def set_vane_direction(self, direction):
        """Change vane direction. Valid directions: horizontal, midhorizontal,
        midpoint, midvertical, vertical, auto, and sometimes swing
        """
        valid_directions = self.get_vane_directions()
        if direction not in valid_directions:
            _LOGGER.warning("Attempting to set an invalid vane direction %s", direction)
            return {}
        command = (
            '{"c": { "indoorUnit": { "status": { "vaneDir": "%s" } } } }' % direction
        ).encode("utf-8")
        response = self._request(command)
        self._status["vaneDir"] = direction
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response

    def set_temp_source(self, source):
        """Change temperature source. Valid sources: sensor0-sensor3,
        returnair, remote, api.
        """
        if source not in SETTABLE_TEMP_SOURCES:
            _LOGGER.warning("Attempting to set invalid temp source %s", source)
            return {}
        command = (
            '{"c": { "indoorUnit": { "status": { "tempSource": "%s" } } } }' % source
        ).encode("utf-8")
        response = self._request(command)
        self._status["tempSource"] = source
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response

    def set_injected_room_temp(self, temperature):
        """Inject a room temperature for the unit to use.

        Only used if tempSource is set to "api". The caller must re-send the value
        periodically to prevent the system from ignoring it as stale.
        """
        temp_source = self.get_temp_source()
        if temp_source != "api":
            _LOGGER.warning(
                "Ignoring injected room temp; tempSource is %s, not 'api'",
                temp_source,
            )
            return {}
        temperature = round(float(temperature), 1)
        command = (
            '{"c": { "indoorUnit": { "status": { "roomTemp": %.1f } } } }' % temperature
        ).encode("utf-8")
        response = self._request(command)
        self._status["roomTemp"] = temperature
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response

    def set_hold(self, end_time):
        """Set a hold on the current temperature until end_time.
        Accepts unix timesamp.
        MHK uses 4294967295 to set 'permanent hold'
        """
        command = (
            '{"c":{"mhk2":{"hold":{"adapter":{"endTime": %d}}}}}' % end_time
        ).encode("utf-8")
        response = self._request(command)
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response

    def send_raw_cn105_frame(self, frame: bytes, id_byte: int = 1) -> bool:
        """Transmit a complete raw CN105 frame onto the indoor unit's bus.

        ``frame`` is the full wire frame (``FC | type | 01 30 | len | payload |
        checksum``). This performs the single compound PUT of
        ``indoorUnit.settings.rawITPFrame`` with ``frame``/``len``/``id`` set
        together; Returns True on success. The frame length must be btwn 1-127.
        """
        frame = bytes(frame)
        length = len(frame)
        if not 1 <= length <= MAX_FRAME_LEN:
            _LOGGER.warning(
                "%s: raw CN105 frame length %d out of range 1..%d",
                self._name,
                length,
                MAX_FRAME_LEN,
            )
            return False
        command = (
            '{"c":{"indoorUnit":{"settings":{"rawITPFrame":'
            '{"frame":"%s","len":%d,"id":%d}}}}}'
            % (frame.hex(), length, id_byte)
        ).encode("utf-8")
        response = self._request(command)
        if not response or "_api_error" in response:
            _LOGGER.warning(
                "%s: failed to send raw CN105 frame: %s", self._name, response
            )
            return False
        return True

    def read_raw_cn105_frame(self) -> bytes | None:
        """Read back the last raw CN105 reply latched by the adapter.

        Returns the reply frame bytes, or None if the buffer is empty or the
        response is malformed.
        """
        query = '{"c":{"indoorUnit":{"settings":{"rawITPFrame":{}}}}}'.encode("utf-8")
        response = self._request(query)
        try:
            node = response["r"]["indoorUnit"]["settings"]["rawITPFrame"]
        except (KeyError, TypeError):
            return None
        hexstr = node.get("frame") if isinstance(node, dict) else None
        if not hexstr:
            return None
        try:
            return bytes.fromhex(hexstr)
        except ValueError:
            _LOGGER.warning(
                "%s: raw CN105 readback is not valid hex: %r", self._name, hexstr
            )
            return None

    def transceive_cn105_frame(
        self,
        frame: bytes,
        id_byte: int = 1,
        expect_type: int | None = None,
        expect_code: int | None = None,
        timeout: float = RAW_REPLY_TIMEOUT_SECONDS,
    ) -> bytes | None:
        """Send a raw CN105 frame once and return the unit's reply frame.

        Transmits ``frame`` a single time, then polls the readback
        buffer for up to ``timeout`` seconds. If ``expect_type``/``expect_code``
        are given, only a reply whose byte[1] == expect_type and byte[5] ==
        expect_code is accepted, so a stale reply for another info code sharing
        the single readback buffer is skipped and polling continues. The reply
        checksum is validated. Returns the reply frame bytes, or None if no
        matching reply arrives within ``timeout``.
        """
        if not self.send_raw_cn105_frame(frame, id_byte):
            return None
        poll_count = max(1, int(timeout / RAW_REPLY_POLL_INTERVAL_SECONDS))
        for _ in range(poll_count):
            time.sleep(RAW_REPLY_POLL_INTERVAL_SECONDS)
            reply = self.read_raw_cn105_frame()
            if not valid_cn105_reply(reply):
                continue
            if expect_type is not None and reply[1] != expect_type:
                continue
            if expect_code is not None and reply[5] != expect_code:
                continue
            return reply
        _LOGGER.debug(
            "%s: no valid CN105 reply within %.1fs", self._name, timeout
        )
        return None

    def get_outdoor_temperature(self) -> float | None:
        """Read outdoor air temperature (C) straight off the CN105 bus.

        Sends a ``0x03`` info request and decodes the outdoor temperature from
        the ``0x62`` reply for the outdoor unit. Returns None if not avavilable
        for whatever reason.
        """
        frame = build_info_request(INFO_CODE_ROOM_TEMP)
        reply = self.transceive_cn105_frame(
            frame, expect_type=INFO_RESPONSE_TYPE, expect_code=INFO_CODE_ROOM_TEMP
        )
        if reply is None:
            return None
        return decode_outdoor_temperature(reply)

    def get_raw_room_temperature(self) -> float | None:
        """Read room temperature (C) straight off the CN105 bus.

        Companion to :meth:`get_outdoor_temperature`; decodes the room
        temperature from the same ``0x03`` reply. Returns None on no reply.
        """
        frame = build_info_request(INFO_CODE_ROOM_TEMP)
        reply = self.transceive_cn105_frame(
            frame, expect_type=INFO_RESPONSE_TYPE, expect_code=INFO_CODE_ROOM_TEMP
        )
        if reply is None:
            return None
        return decode_room_temperature(reply)

    def get_operating(self) -> bool | None:
        """Read the operating flag straight off the CN105 bus.

        Sends a ``0x06`` info request and decodes the operating byte from the
        ``0x62`` reply: True = compressor running, False = standby. Returns None
        on an invalid frame, or if the unit does not support the ``0x06`` status
        frame, which just results in this timing out and may require an adapter
        reboot(?) to unblock other cn105 commands.
        """
        reply = self.transceive_cn105_frame(
            build_info_request(INFO_CODE_STATUS),
            expect_type=INFO_RESPONSE_TYPE,
            expect_code=INFO_CODE_STATUS,
        )
        if reply is None:
            return None
        return decode_operating(reply)

    def get_compressor_frequency(self) -> int | None:
        """Read the compressor frequency (Hz).
        """
        reply = self.transceive_cn105_frame(
            build_info_request(INFO_CODE_STATUS),
            expect_type=INFO_RESPONSE_TYPE,
            expect_code=INFO_CODE_STATUS,
        )
        if reply is None:
            return None
        return decode_compressor_frequency(reply)

    def _read_standby_reply(self) -> bytes | None:
        """Reads ``0x09`` standby reply frame"""
        return self.transceive_cn105_frame(
            build_info_request(INFO_CODE_STANDBY),
            expect_type=INFO_RESPONSE_TYPE,
            expect_code=INFO_CODE_STANDBY,
        )

    def get_sub_mode(self) -> str | None:
        """Read the sub mode.

        """
        reply = self._read_standby_reply()
        if reply is None:
            return None
        return decode_sub_mode(reply)

    def get_stage(self) -> str | None:
        """Read the indoor fan stage.

        """
        reply = self._read_standby_reply()
        if reply is None:
            return None
        return decode_stage(reply)

    def get_auto_sub_mode(self) -> str | None:
        """Read the auto sub mode.
        """
        reply = self._read_standby_reply()
        if reply is None:
            return None
        return decode_auto_sub_mode(reply)

    def get_compressor_runtime_minutes(self) -> int | None:
        """Read the compressor runtime counter (minutes).
        The counter advances only while the compressor is actually running.
        """
        reply = self.transceive_cn105_frame(
            build_info_request(INFO_CODE_ROOM_TEMP),
            expect_type=INFO_RESPONSE_TYPE,
            expect_code=INFO_CODE_ROOM_TEMP,
        )
        if reply is None:
            return None
        return decode_compressor_runtime_minutes(reply)

    def get_compressor_runtime_hours(self) -> float | None:
        """Read the compressor runtime counter (hours).

        Companion to :meth:`get_compressor_runtime_minutes` that returns the
        counter in hours. Returns None on no reply or an invalid frame.
        """
        reply = self.transceive_cn105_frame(
            build_info_request(INFO_CODE_ROOM_TEMP),
            expect_type=INFO_RESPONSE_TYPE,
            expect_code=INFO_CODE_ROOM_TEMP,
        )
        if reply is None:
            return None
        return decode_compressor_runtime_hours(reply)

    def get_status_0x03(self) -> dict | None:
        """Reads room/outdoor/runtime.
        """
        reply = self.transceive_cn105_frame(
            build_info_request(INFO_CODE_ROOM_TEMP),
            expect_type=INFO_RESPONSE_TYPE,
            expect_code=INFO_CODE_ROOM_TEMP,
        )
        if reply is None:
            return None
        return decode_status_0x03(reply)

    def get_status_0x09(self) -> dict | None:
        """Reads standby status.

        """
        reply = self._read_standby_reply()
        if reply is None:
            return None
        return decode_status_0x09(reply)

    def get_status_0x06(self, timeout: float = RAW_REPLY_TIMEOUT_SECONDS) -> dict | None:
        """Read the full ``0x06`` status in one round trip.

        Only on supported units. For unsupported units, this is just going to time out
        and worse may block other cn105 commands until the adapter is rebooted.
        """
        reply = self.transceive_cn105_frame(
            build_info_request(INFO_CODE_STATUS),
            expect_type=INFO_RESPONSE_TYPE,
            expect_code=INFO_CODE_STATUS,
            timeout=timeout,
        )
        if reply is None:
            return None
        return decode_status_0x06(reply)

    def get_conditioning_activity(self, sample_interval: float = 75.0) -> dict:
        """Estimate whether the unit is actually heating/cooling right now.
        
        This refreshes quite slowly compared to 0x06's operating field, so that's
        definitely preferred. But this can be a good fallback when 0x06 is not supported.

        Reads the compressor runtime counter twice, ``sample_interval`` seconds
        apart, and reports the unit as active when the counter advanced. Returns
        ``{"active", "activity", "mode", "delta_minutes", "sample_interval"}``.

        When :meth:`get_mode` is ``'off'`` this returns immediately without
        sampling. Otherwise ``active`` is True when the runtime delta is > 0, and
        ``activity`` is one of ``"cooling"``, ``"heating"``, ``"conditioning"``
        (mode ``'auto'`` or other, direction ambiguous from runtime alone),
        ``"idle"`` (on but no compressor demand), ``"off"``, or ``"unknown"``
        (a runtime read failed).

        """
        mode = self.get_mode()
        if mode == "off":
            return {
                "active": False,
                "activity": "off",
                "mode": "off",
                "delta_minutes": 0,
                "sample_interval": 0,
            }
        m0 = self.get_compressor_runtime_minutes()
        time.sleep(sample_interval)
        m1 = self.get_compressor_runtime_minutes()
        if m0 is None or m1 is None:
            return {
                "active": None,
                "activity": "unknown",
                "mode": mode,
                "delta_minutes": None,
                "sample_interval": sample_interval,
            }
        delta = m1 - m0
        active = delta > 0
        if not active:
            activity = "idle"
        elif mode == "cool":
            activity = "cooling"
        elif mode == "heat":
            activity = "heating"
        else:
            activity = "conditioning"
        return {
            "active": active,
            "activity": activity,
            "mode": mode,
            "delta_minutes": delta,
            "sample_interval": sample_interval,
        }

    def do_reboot(self):
        """Issue a reboot command to the indoor unit's adapter."""
        command = ('{"c":{"adapter":{"status":{"runState":"reboot"}}}}').encode("utf-8")
        response = self._request(command)
        self._last_status_update = time.monotonic() - 2 * CACHE_INTERVAL_SECONDS
        return response
