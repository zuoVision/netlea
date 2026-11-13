"""Protocol for the Netlea G1 Bluetooth filter series."""

from __future__ import annotations

from datetime import datetime

KEY = b"Leds&Fun"


def _check_byte(data: bytes) -> int:
    result = 0
    for value in data:
        for key_byte in KEY:
            if value & 1:
                result ^= key_byte
            value >>= 1
    return result


def encode(data: bytes) -> bytes:
    check = _check_byte(data)
    return bytes([check, *(check ^ value for value in data)])


def decode(data: bytes) -> bytes:
    if not data:
        return b""
    check = data[0]
    plain = bytes(check ^ value for value in data[1:])
    if _check_byte(plain) != check:
        raise ValueError("Netlea frame check byte mismatch")
    return plain


def control_frame(*, is_on: bool, gear: int, mode: int = 1) -> bytes:
    """Build EEE1 frame. Mode 1 is constant flow; 2 is wave; 3 is feeding."""
    gear = max(0, min(20, int(gear)))
    # The live vendor-app control path sends 10 fields followed by two reserved
    # bytes. Sending the nominal four-byte restoreActTime makes this G1 firmware
    # reject the packet and disconnect without applying it.
    plain = bytes(
        [
            2, int(is_on), 0, 0, 1, mode, gear if is_on else 0,
            0, 0, 0, 0, 0,
        ]
    )
    return encode(plain)


def query_frame() -> bytes:
    return encode(bytes([2, 2]))


def app_control_frame() -> bytes:
    return encode(bytes([2, 4]))


def init_frame(now: datetime | None = None) -> bytes:
    now = now or datetime.now()
    return encode(
        bytes(
            [
                0, 1, now.year - 2000, now.month, now.day,
                now.hour, now.minute, now.second, 0,
            ]
        )
    )


def _time_parts(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time: {value!r}")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid time: {value!r}")
    return hour, minute


def trip_frame(trips: list[dict]) -> bytes:
    """Build the EEE2 daily table used by the vendor app (maximum 10 rows)."""
    if len(trips) > 10:
        raise ValueError("The filter supports at most 10 trips")
    normalized = []
    for trip in trips:
        start = _time_parts(str(trip["start"]))
        end = _time_parts(str(trip["end"]))
        if start >= end:
            raise ValueError("Trip end time must be after start time")
        normalized.append((start, end, trip))
    normalized.sort(key=lambda item: item[0])
    for previous, current in zip(normalized, normalized[1:]):
        if current[0] < previous[1]:
            raise ValueError("Trip times must not overlap")

    plain = [0, 0, 0, 0, 0, 0]
    for (start_h, start_m), (end_h, end_m), trip in normalized:
        gear = max(1, min(20, int(trip["gear"])))
        mode = max(1, min(3, int(trip.get("mode", 1))))
        enabled = 1 if trip.get("enabled", True) else 0
        plain.extend([enabled, start_h, start_m, end_h, end_m, mode, gear, 0, 0, 0])
    for _ in range(10 - len(trips)):
        plain.extend([0, 0, 0, 23, 59, 0, 0, 0, 0, 0])
    return encode(bytes(plain))


def parse_trip_table(frame: bytes) -> list[dict[str, str | int]]:
    plain = decode(frame)
    if len(plain) < 106:
        raise ValueError(f"Short Netlea trip table: {len(plain)}")
    trips = []
    for offset in range(6, 106, 10):
        row = plain[offset : offset + 10]
        # Empty filler rows have neither a mode nor a gear. A disabled trip has
        # tripDisable=0 but keeps its times/mode/gear so it can be re-enabled.
        if row[5] == 0 and row[6] == 0:
            continue
        trips.append(
            {
                "start": f"{row[1]:02d}:{row[2]:02d}",
                "end": f"{row[3]:02d}:{row[4]:02d}",
                "mode": row[5],
                "gear": row[6],
                "enabled": bool(row[0]),
            }
        )
    return trips


def parse_status(frame: bytes) -> dict[str, int | bool]:
    plain = decode(frame)
    if len(plain) < 8:
        raise ValueError(f"Short Netlea status frame: {len(plain)}")
    return {
        "device_model": plain[0],
        "control_model": plain[1],
        "mode": plain[2],
        "gear": plain[3],
        "level": plain[4],
        "scheduled": plain[5],
        "work_model": plain[6],
        "error_code": plain[7],
        "is_on": plain[3] > 0 and plain[2] not in (4, 6, 7),
    }
