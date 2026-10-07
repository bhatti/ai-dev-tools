# SPDX-License-Identifier: AGPL-3.0-or-later
"""Convert Slack emoji shortcodes to Unicode characters for HTML/Markdown rendering.

Slack shortcodes (e.g. :rocket:) work in Slack mrkdwn but appear as literal text in
HTML and Markdown reports. Call convert_slack_emoji() on any content before rendering
to HTML or writing to .md files.
"""
from __future__ import annotations

import re

# Comprehensive map of Slack shortcodes → Unicode emoji.
# Covers all shortcodes that appear in AI-generated content (standup synthesis,
# PR audit, codebase audit, resync reports, skill outputs, etc.).
SLACK_EMOJI: dict[str, str] = {
    # Status / health
    ":white_check_mark:":       "✅",
    ":heavy_check_mark:":       "✔️",
    ":x:":                      "❌",
    ":warning:":                "⚠️",
    ":rotating_light:":         "🚨",
    ":no_entry:":               "⛔",
    ":no_entry_sign:":          "🚫",
    ":construction:":           "🚧",
    ":stop_sign:":              "🛑",
    ":question:":               "❓",
    ":exclamation:":            "❗",
    ":bangbang:":               "‼️",
    ":grey_question:":          "❔",
    # Circles / indicators
    ":red_circle:":             "🔴",
    ":large_red_circle:":       "🔴",
    ":orange_circle:":          "🟠",
    ":yellow_circle:":          "🟡",
    ":large_yellow_circle:":    "🟡",
    ":green_circle:":           "🟢",
    ":large_green_circle:":     "🟢",
    ":blue_circle:":            "🔵",
    ":large_blue_circle:":      "🔵",
    ":purple_circle:":          "🟣",
    ":brown_circle:":           "🟤",
    ":white_circle:":           "⚪",
    ":black_circle:":           "⚫",
    # People / roles
    ":bust_in_silhouette:":     "👤",
    ":busts_in_silhouette:":    "👥",
    ":person_raising_hand:":    "🙋",
    ":wave:":                   "👋",
    ":eyes:":                   "👀",
    ":handshake:":              "🤝",
    ":pray:":                   "🙏",
    ":+1:":                     "👍",
    ":thumbsup:":               "👍",
    ":-1:":                     "👎",
    ":thumbsdown:":             "👎",
    # Actions / work
    ":rocket:":                 "🚀",
    ":hammer:":                 "🔨",
    ":hammer_and_wrench:":      "🛠️",
    ":wrench:":                 "🔧",
    ":nut_and_bolt:":           "🔩",
    ":gear:":                   "⚙️",
    ":pencil:":                 "📝",
    ":pencil2:":                "✏️",
    ":memo:":                   "📝",
    ":writing_hand:":           "✍️",
    ":mag:":                    "🔍",
    ":mag_right:":              "🔎",
    ":microscope:":             "🔬",
    ":test_tube:":              "🧪",
    ":fire:":                   "🔥",
    ":zap:":                    "⚡",
    ":tada:":                   "🎉",
    ":sparkles:":               "✨",
    ":star:":                   "⭐",
    ":star2:":                  "🌟",
    ":trophy:":                 "🏆",
    ":medal:":                  "🏅",
    ":checkered_flag:":         "🏁",
    ":triangular_flag_on_post:":"🚩",
    # Documents / data
    ":page_facing_up:":         "📄",
    ":page_with_curl:":         "📃",
    ":books:":                  "📚",
    ":book:":                   "📖",
    ":notebook:":               "📓",
    ":clipboard:":              "📋",
    ":paperclip:":              "📎",
    ":link:":                   "🔗",
    ":chains:":                 "⛓️",
    ":file_folder:":            "📁",
    ":open_file_folder:":       "📂",
    ":inbox_tray:":             "📥",
    ":outbox_tray:":            "📤",
    ":package:":                "📦",
    # Communication
    ":speech_balloon:":         "💬",
    ":thought_balloon:":        "💭",
    ":mega:":                   "📣",
    ":loudspeaker:":            "📢",
    ":bell:":                   "🔔",
    ":no_bell:":                "🔕",
    ":mailbox:":                "📫",
    ":email:":                  "📧",
    # Charts / metrics
    ":bar_chart:":              "📊",
    ":chart_with_upwards_trend:":"📈",
    ":chart_with_downwards_trend:":"📉",
    ":abacus:":                 "🧮",
    # Time
    ":calendar:":               "📅",
    ":spiral_calendar_pad:":    "🗓️",
    ":spiral_calendar:":        "🗓️",
    ":clock1:":                 "🕐",
    ":clock2:":                 "🕑",
    ":clock3:":                 "🕒",
    ":clock4:":                 "🕓",
    ":stopwatch:":              "⏱️",
    ":timer_clock:":            "⏲️",
    ":hourglass:":              "⏳",
    ":hourglass_flowing_sand:": "⏳",
    ":alarm_clock:":            "⏰",
    # Arrows / direction
    ":arrow_right:":            "→",
    ":arrow_left:":             "←",
    ":arrow_up:":               "↑",
    ":arrow_down:":             "↓",
    ":arrow_upper_right:":      "↗",
    ":arrow_upper_left:":       "↖",
    ":arrow_lower_right:":      "↘",
    ":arrow_lower_left:":       "↙",
    ":left_right_arrow:":       "↔",
    ":arrows_counterclockwise:":"🔄",
    ":repeat:":                 "🔁",
    ":twisted_rightwards_arrows:":"🔀",
    ":fast_forward:":           "⏩",
    ":rewind:":                 "⏪",
    # Security / infra
    ":lock:":                   "🔒",
    ":unlock:":                 "🔓",
    ":shield:":                 "🛡️",
    ":key:":                    "🔑",
    ":old_key:":                "🗝️",
    ":closed_lock_with_key:":   "🔐",
    ":computer:":               "💻",
    ":desktop_computer:":       "🖥️",
    ":keyboard:":               "⌨️",
    ":floppy_disk:":            "💾",
    ":cd:":                     "💿",
    ":electric_plug:":          "🔌",
    ":battery:":                "🔋",
    ":satellite:":              "📡",
    # Misc UI
    ":information_source:":     "ℹ️",
    ":bulb:":                   "💡",
    ":idea:":                   "💡",
    ":new:":                    "🆕",
    ":up:":                     "🆙",
    ":sos:":                    "🆘",
    ":recycle:":                "♻️",
    ":globe_with_meridians:":   "🌐",
    ":world_map:":              "🗺️",
    ":pushpin:":                "📌",
    ":round_pushpin:":          "📍",
    ":label:":                  "🏷️",
    ":ticket:":                 "🎫",
    ":bookmark:":               "🔖",
    ":scissors:":               "✂️",
    ":wastebasket:":            "🗑️",
    ":speech_bubble:":          "💬",
    # Git/code specific
    ":octocat:":                "🐙",
    ":bug:":                    "🐛",
    ":spider:":                 "🕷️",
    ":ant:":                    "🐜",
}

# Pre-compiled pattern matching any :shortcode: token
_SHORTCODE_RE = re.compile(
    r":[a-zA-Z0-9_+\-]+:"
)


def convert_slack_emoji(text: str) -> str:
    """Replace all Slack emoji shortcodes with their Unicode equivalents.

    Unknown shortcodes are left unchanged (they may be legitimate colons or
    other syntax that should not be mangled).
    """
    def _replace(m: re.Match) -> str:
        return SLACK_EMOJI.get(m.group(0), m.group(0))
    return _SHORTCODE_RE.sub(_replace, text)
