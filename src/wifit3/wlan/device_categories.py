"""Emoji shown for a client whose exact device isn't known, but whose OUI has a broad category
(from fingerprint_categories.py, GENERATED -- see scripts/generators/gen_fingerprint_categories.py).
No logic here, just this one mapping: if an emoji doesn't feel right, or a category from the
source data is missing, change/add it here directly. Categories come from a third-party dataset
(OUI-Master-Database, itself aggregated from several public sources) that classifies roughly one
in six OUIs; there's no authoritative emoji-per-category standard to defer to, so this is a
starting point, not a final answer -- happy to take a second (or twentieth) opinion on any of it.
"""
from __future__ import annotations

CATEGORY_EMOJI: dict[str, str] = {
    "Phone": "📱",
    "Tablet": "📱",
    "Router": "🌐",
    "Access Point": "📡",
    "Modem": "📶",
    "Computer": "💻",
    "Server": "💻",
    "Smart Home": "🏠",
    "IoT": "🔗",
    "Automotive": "🚗",
    "Industrial": "🏭",
    "Switch": "🔀",
    "Storage": "💾",
    "Medical": "🩺",
    "Camera": "📷",
    "TV": "📺",
    "Media Player": "📺",
    "Gaming": "🎮",
    "Appliance": "🧺",
    "VoIP": "☎️",
    "Printer": "🖨️",
    "Wearable": "⌚",
    "Audio": "🔊",
    "Thermostat": "🌡️",
}
