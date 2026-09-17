"""V5 output-name frames and complete, byte-preserving name snapshots."""
from dataclasses import dataclass
import hashlib

from .snapshot import InvalidSnapshot


def pack_station_name(station: int, name: str, physical_stations: int) -> list[bytes]:
    """Encode a physical output name as two 0x33 frames (no manual commit)."""
    if type(station) is not int or not 1 <= station <= physical_stations <= 12:
        raise ValueError("Invalid station")
    if not isinstance(name, str) or not name.strip() or "\0" in name:
        raise ValueError("Enter a non-empty name without NUL characters")
    encoded = name.encode("utf-8")
    if len(encoded) > 32:
        raise ValueError("Station names must fit 32 UTF-8 bytes")
    padded = encoded.ljust(32, b"\0")
    return [bytes([0x33, 0x12, part, station - 1]) + padded[part * 16:(part + 1) * 16]
            for part in (0, 1)]


@dataclass(frozen=True)
class StationNameSnapshot:
    """Names for every reported output, including unused physical slots."""
    raw_names: dict[int, bytes]

    @classmethod
    def from_frames(cls, frames: list[bytes], physical_stations: int) -> "StationNameSnapshot":
        parts: dict[int, dict[int, bytes]] = {}
        sequences: set[int] = set()
        for frame in frames:
            if len(frame) != 20 or frame[:2] not in (b"\x36\x12", b"\x35\x12") or frame[3] >= 12:
                raise InvalidSnapshot("Invalid station-name response")
            station, part = frame[3] + 1, frame[2] & 1
            group = parts.setdefault(station, {})
            if part in group and group[part] != frame[4:]:
                raise InvalidSnapshot("Conflicting station-name fragments")
            group[part] = frame[4:]
            sequences.add(frame[2])
        if (not set(range(1, physical_stations + 1)) <= parts.keys()
            or any(set(group) != {0, 1} for group in parts.values())
            or sequences != set(range(max(sequences, default=-1) + 1))):
            raise InvalidSnapshot("Incomplete station-name response")
        return cls({station: group[1] + group[0] for station, group in parts.items()})

    @property
    def names(self) -> dict[int, str]:
        return {station: raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
                for station, raw in self.raw_names.items()}

    @property
    def revision(self) -> str:
        return hashlib.sha256(b"".join(bytes([station]) + raw for station, raw in sorted(self.raw_names.items()))).hexdigest()

    def renamed(self, station: int, name: str, physical_stations: int) -> "StationNameSnapshot":
        frames = pack_station_name(station, name, physical_stations)
        return StationNameSnapshot({**self.raw_names, station: frames[0][4:] + frames[1][4:]})
