"""LoRa PHY airtime estimator for a given radio packet byte count.

Computes Semtech explicit-header CSS formula with PHY CRC enabled.
Does not inspect or transmit to the attached MeshCore radio.
"""

from math import ceil
import re

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

SETTINGS = {
    "sf": ("default_sf", 7, 12),
    "bw": ("default_bw_khz", 62.5, 500),
    "cr": ("default_cr_denominator", 5, 8),
    "pre": ("default_preamble_symbols", 6, 32),
    "tx": ("default_transmissions", 1, 20),
}


def lora_airtime_ms(payload_bytes: int, *, sf: int, bw: int, cr: int, pre: int) -> float:
    """LoRa airtime per RF transmission in milliseconds.

    - BW is kHz; cr is 5..8 for coding rate 4/5..4/8.
    - Explicit header (IH=0); PHY CRC on (CRC=1).
    - Low data rate optimization when symbol duration >= 16ms.
    """
    t_symbol = (2 ** sf) / (bw * 1000.0)
    de = int(t_symbol >= 0.016)
    numerator = 8 * payload_bytes - 4 * sf + 28 + 16
    denominator = 4 * (sf - 2 * de)
    payload_symbols = 8 + max(0, ceil(numerator / denominator) * cr)
    return (pre + 4.25 + payload_symbols) * t_symbol * 1000


class AirTimeCommand(BaseCommand):
    name = "airtime"
    keywords = ["airtime"]
    description = "Estimate LoRa packet airtime from PHY size and modem settings"
    short_description = "Approximate LoRa RF airtime without transmitting"
    usage = "airtime BYTES [sf=7 bw=62.5 cr=5 pre=32 tx=1]"
    examples = ["airtime 80", "airtime 80 sf=7 bw=62.5 cr=5 tx=3"]
    category = "meshcore_info"
    requires_dm = True
    cooldown_seconds = 10

    def __init__(self, bot):
        super().__init__(bot)
        self.command_enabled = self.get_config_value(
            "Airtime_Command", "enabled", fallback=True, value_type="bool"
        )

    def can_execute(self, message, skip_channel_check=False):
        return self.command_enabled and super().can_execute(message, skip_channel_check=skip_channel_check)

    def get_help_text(self):
        return "DM: airtime BYTES [sf=7 bw=62.5 cr=5 pre=32 tx=1]. Bytes=whole LoRa packet; verify actual modem profile."

    async def _reply(self, message, text):
        budget = self.get_max_message_length(message)
        if len(text.encode("utf-8")) > budget:
            text = self.get_help_text()
        return await self.send_response(message, text[:budget])

    def _defaults(self):
        vals = {}
        initial = {"sf": 7, "bw": 62.5, "cr": 5, "pre": 32, "tx": 1}
        for k, (field, low, high) in SETTINGS.items():
            val = self.get_config_value("Airtime_Command", field, fallback=initial[k], value_type="str" if k == "bw" else "int")
            try:
                val = float(val) if k == "bw" else int(val)
            except (TypeError, ValueError):
                val = initial[k]
            if k == "bw" and val not in (62.5, 125, 250, 500):
                val = initial[k]
            elif not low <= val <= high:
                val = initial[k]
            vals[k] = val
        return vals

    async def execute(self, message: MeshMessage) -> bool:
        if not message.is_dm:
            return True
        tokens = (message.content or "").strip().removeprefix("!").split()
        if len(tokens) < 2 or not tokens[1].isdigit():
            return await self._reply(message, self.get_help_text())
        size = int(tokens[1])
        if not 1 <= size <= 255:
            return await self._reply(message, "Airtime: BYTES must be 1-255 LoRa PHY bytes")
        values = self._defaults()
        seen = set()
        for token in tokens[2:]:
            match = re.fullmatch(r"(sf|bw|cr|pre|tx)=(\d+(?:\.\d+)?)", token, re.IGNORECASE)
            if not match:
                return await self._reply(message, self.get_help_text())
            key = match.group(1).lower()
            if key != "bw" and "." in match.group(2):
                return await self._reply(message, self.get_help_text())
            try:
                value = float(match.group(2)) if key == "bw" else int(match.group(2))
            except ValueError:
                return await self._reply(message, self.get_help_text())
            if key in seen:
                return await self._reply(message, "Airtime: duplicate option")
            seen.add(key)
            _, low, high = SETTINGS[key]
            if not low <= value <= high or (key == "bw" and value not in (62.5, 125, 250, 500)):
                return await self._reply(message, f"Airtime: invalid {key}; see !help airtime")
            values[key] = value
        air_ms = lora_airtime_ms(size, **{k: values[k] for k in ("sf", "bw", "cr", "pre")})
        s = f"{air_ms / 1000:.3f}s"
        suffix = f" / {values['tx']} TX sum {air_ms * values['tx']/1000:.2f}s" if values["tx"] > 1 else ""
        return await self._reply(message, (
            f"LoRa {size}B SF{values['sf']} BW{values['bw']:g}k CR4/{values['cr']} pre{values['pre']}\n"
            f"Airtime: {s}{suffix}\nEstimated PHY TX; no retries counted"
        ))