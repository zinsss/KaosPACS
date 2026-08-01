from __future__ import annotations

import html
import io
import struct
from dataclasses import dataclass
from typing import Any

from pydicom import dcmread


TWELVE_LEAD_ECG_WAVEFORM_STORAGE = "1.2.840.10008.5.1.4.1.1.9.1.1"
MAX_POINTS_PER_LEAD = 900
TEXT_VR_NAMES = {
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
}


@dataclass(frozen=True)
class WaveformLead:
    label: str
    samples: list[float]


@dataclass(frozen=True)
class WaveformPreview:
    svg: bytes
    content_type: str = "image/svg+xml; charset=utf-8"


def render_ecg_waveform_svg(dicom_bytes: bytes) -> WaveformPreview | None:
    dataset = dcmread(io.BytesIO(dicom_bytes), stop_before_pixels=True)
    if str(getattr(dataset, "SOPClassUID", "")) != TWELVE_LEAD_ECG_WAVEFORM_STORAGE:
        return None
    groups = list(getattr(dataset, "WaveformSequence", []) or [])
    if not groups:
        return None

    group = _select_waveform_group(groups)
    leads = _extract_leads(group)
    if not leads:
        return None

    svg = _render_svg(leads)
    return WaveformPreview(svg=svg.encode("utf-8"))


def _select_waveform_group(groups: list[Any]) -> Any:
    return max(
        groups,
        key=lambda item: int(getattr(item, "NumberOfWaveformSamples", 0) or 0),
    )


def _extract_leads(group: Any) -> list[WaveformLead]:
    channel_count = int(getattr(group, "NumberOfWaveformChannels", 0) or 0)
    sample_count = int(getattr(group, "NumberOfWaveformSamples", 0) or 0)
    bits = int(getattr(group, "WaveformBitsAllocated", 0) or 0)
    interpretation = str(getattr(group, "WaveformSampleInterpretation", "") or "")
    waveform_data = bytes(getattr(group, "WaveformData", b"") or b"")
    if channel_count <= 0 or sample_count <= 0 or bits != 16 or not waveform_data:
        return []
    fmt = "<h" if interpretation.upper() == "SS" else "<H"
    expected_values = channel_count * sample_count
    available_values = len(waveform_data) // 2
    usable_values = min(expected_values, available_values)
    values = [
        struct.unpack_from(fmt, waveform_data, offset)[0]
        for offset in range(0, usable_values * 2, 2)
    ]

    labels = _channel_labels(group, channel_count)
    leads: list[WaveformLead] = []
    step = max(1, sample_count // MAX_POINTS_PER_LEAD)
    for channel_index in range(channel_count):
        samples = [
            float(values[sample_index * channel_count + channel_index])
            for sample_index in range(0, sample_count, step)
            if sample_index * channel_count + channel_index < len(values)
        ]
        if samples:
            leads.append(WaveformLead(labels[channel_index], samples))
    return leads


def _channel_labels(group: Any, channel_count: int) -> list[str]:
    labels = [f"Lead {index + 1}" for index in range(channel_count)]
    channels = list(getattr(group, "ChannelDefinitionSequence", []) or [])
    for index, channel in enumerate(channels[:channel_count]):
        source = list(getattr(channel, "ChannelSourceSequence", []) or [])
        if source:
            label = str(
                getattr(source[0], "CodeMeaning", "")
                or getattr(source[0], "CodeValue", "")
                or ""
            ).strip()
            if label:
                labels[index] = label
    return labels


def _render_svg(leads: list[WaveformLead]) -> str:
    width = 1100
    row_height = 86
    left = 76
    right = 24
    top = 28
    height = top + row_height * len(leads) + 28
    plot_width = width - left - right
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="ECG waveform preview">',
        "<style>"
        ".bg{fill:#2E3440}.grid{stroke:#4C566A;stroke-width:1;opacity:.55}"
        ".fine{stroke:#434C5E;stroke-width:1;opacity:.35}.trace{fill:none;stroke:#88C0D0;stroke-width:1.45}"
        ".label{fill:#ECEFF4;font:700 15px system-ui,-apple-system,Segoe UI,sans-serif}"
        "</style>",
        f'<rect class="bg" x="0" y="0" width="{width}" height="{height}"/>',
    ]
    for x in range(left, width - right + 1, 20):
        parts.append(f'<line class="fine" x1="{x}" y1="{top}" x2="{x}" y2="{height - 22}"/>')
    for x in range(left, width - right + 1, 100):
        parts.append(f'<line class="grid" x1="{x}" y1="{top}" x2="{x}" y2="{height - 22}"/>')
    for index, lead in enumerate(leads):
        y_mid = top + index * row_height + row_height / 2
        parts.append(f'<line class="grid" x1="{left}" y1="{y_mid:.1f}" x2="{width - right}" y2="{y_mid:.1f}"/>')
        parts.append(
            f'<text class="label" x="16" y="{y_mid + 5:.1f}">{html.escape(lead.label)}</text>'
        )
        path = _lead_path(lead.samples, left, y_mid, plot_width, row_height * 0.34)
        if path:
            parts.append(f'<path class="trace" d="{path}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _lead_path(samples: list[float], x0: float, y_mid: float, width: float, max_amp: float) -> str:
    if len(samples) < 2:
        return ""
    centered = _center_samples(samples)
    peak = max(max(abs(value) for value in centered), 1.0)
    scale = max_amp / peak
    x_step = width / max(1, len(centered) - 1)
    coords = []
    for index, value in enumerate(centered):
        x = x0 + index * x_step
        y = y_mid - value * scale
        coords.append(("M" if index == 0 else "L") + f"{x:.1f},{y:.1f}")
    return " ".join(coords)


def _center_samples(samples: list[float]) -> list[float]:
    sorted_samples = sorted(samples)
    median = sorted_samples[len(sorted_samples) // 2]
    return [value - median for value in samples]
