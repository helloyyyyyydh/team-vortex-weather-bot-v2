import hashlib
import json
import os
import random
import time
import urllib.request
from urllib.error import HTTPError
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = Path("bot_state.json")
TIMEOUT = 25
MAX_SEEN = 3000
UTC = timezone.utc
NA = "Not Available"

NWS_URL = "https://api.weather.gov/alerts/active"
METEOALARM_COUNTRIES = [
    "andorra", "austria", "belgium", "bosnia-herzegovina", "bulgaria",
    "croatia", "cyprus", "czechia", "denmark", "estonia", "finland",
    "france", "germany", "greece", "hungary", "iceland", "ireland",
    "israel", "italy", "latvia", "lithuania", "luxembourg", "malta",
    "moldova", "montenegro", "netherlands", "republic-of-north-macedonia",
    "norway", "poland", "portugal", "romania", "serbia", "slovakia",
    "slovenia", "spain", "sweden", "switzerland", "ukraine",
    "united-kingdom",
]
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"

USER_AGENT = "TeamVortexWeatherBot/3.2"

FAKE_CITIES = [
    "Cloudberry Bay, Exampleland",
    "North Nimbus, Fictionia",
    "Stormhaven, Imaginary Republic",
    "Cumulus Crossing, Testoria",
    "Thunderfield, Placeholder Islands",
    "Rainy Ridge, Mockland",
]

FAKE_CATEGORIES = [
    "Fictional Thunderstorm Warning",
    "Imaginary Flood Watch",
    "Simulated High Wind Advisory",
    "Fictional Tornado Watch",
    "Imaginary Coastal Flood Warning",
    "Simulated Extreme Rainfall Alert",
]


def now_utc():
    return datetime.now(UTC)


def parse_time(value):
    if not value:
        return None

    text = str(value).strip()

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (ValueError, TypeError, OverflowError):
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    return dt


def display_time(value):
    """Display 12-hour time and a numeric UTC offset."""
    dt = value if isinstance(value, datetime) else parse_time(value)

    if dt is None:
        return NA

    offset = dt.strftime("%z")
    if len(offset) == 5:
        offset = offset[:3] + ":" + offset[3:]
    elif not offset:
        offset = NA

    zone = " UTC" if dt.utcoffset() == timezone.utc.utcoffset(dt) else ""

    return (
        dt.strftime("%b %d, %Y at %I:%M:%S %p")
        + zone
        + f" ({offset})"
    )


def available(value):
    """Return a clean value or Not Available for missing fields."""
    if value is None:
        return NA

    value = str(value).strip()
    return value if value else NA


def fetch(url, accept="*/*"):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
    )

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object.")
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot safely read {STATE_FILE}: {exc}"
        ) from exc

    state.setdefault("seen", [])
    state.setdefault("last_hourly_test", "")
    state.setdefault("baseline_saved", False)

    if not isinstance(state["seen"], list):
        raise RuntimeError("Invalid alert history in bot_state.json.")

    return state


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN:]
    temporary = STATE_FILE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    temporary.replace(STATE_FILE)


def post_discord(message):
    if not WEBHOOK:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL is missing from GitHub Actions secrets."
        )

    payload = json.dumps({
        "content": message[:1900],
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")

    # Discord may briefly rate-limit a webhook when several alerts arrive
    # together. Respect retry_after and retry instead of failing the whole run.
    for attempt in range(5):
        request = urllib.request.Request(
            WEBHOOK,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com, 1.0)",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(
                        f"Discord returned HTTP {response.status}"
                    )
                return
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")

            if exc.code == 429 and attempt < 4:
                try:
                    error_data = json.loads(details)
                    delay = float(error_data.get("retry_after", 1))
                except (ValueError, TypeError, AttributeError):
                    delay = 1.0
                time.sleep(max(0.5, min(delay, 30.0)))
                continue

            # Never print the webhook URL or other secret data.
            raise RuntimeError(
                f"Discord HTTP {exc.code}: {details[:1000]}"
            ) from exc

    raise RuntimeError("Discord remained rate-limited after 5 attempts.")

def make_id(source, value):
    return hashlib.sha256(
        f"{source}|{value}".encode("utf-8")
    ).hexdigest()


def child_text(element, names):
    wanted = set(names)

    for child in element.iter():
        if child.tag.split("}")[-1] in wanted and child.text:
            value = child.text.strip()
            if value:
                return value

    return ""


def get_nws_alerts():
    data = json.loads(fetch(
        NWS_URL,
        "application/geo+json, application/json",
    ).decode("utf-8"))

    alerts = []

    for feature in data.get("features", []):
        p = feature.get("properties") or {}

        # Filter diagnostic, test, and non-public NWS records.
        status = str(p.get("status") or "").strip().lower()
        scope = str(p.get("scope") or "").strip().lower()
        message_type = str(p.get("messageType") or "").strip().lower()
        event_name = str(p.get("event") or "").strip().lower()
        feature_id = str(feature.get("id") or p.get("id") or "")

        if status and status != "actual":
            continue
        if scope and scope != "public":
            continue
        if message_type and message_type not in {"alert", "update", "cancel"}:
            continue
        if "keepalive" in feature_id.lower() or event_name in {
            "test message", "system message", "test",
        }:
            continue

        source_id = (
            feature.get("id")
            or p.get("id")
            or json.dumps(p, sort_keys=True)
        )

        alerts.append({
            "id": make_id("NWS", source_id),
            "source": "U.S. National Weather Service",
            "event": available(p.get("event")),
            "headline": available(p.get("headline")),
            "description": available(p.get("description")),
            "area": available(p.get("areaDesc")),
            "severity": available(p.get("severity")),
            "urgency": available(p.get("urgency")),
            "certainty": available(p.get("certainty")),
            "start": p.get("onset") or p.get("effective"),
            "end": p.get("ends") or p.get("expires"),
            # API links return JSON; provide a readable NWS website too.
            "url": available(feature.get("id")),
            "human_url": "https://www.weather.gov/",
            "kind": "weather",
        })

    return alerts


def get_europe_alerts():
    alerts = []
    current = now_utc()
    successful_feeds = 0
    feed_errors = []

    # MeteoAlarm provides separate Atom feeds by country, not one
    # aggregate "Europe" feed. Check the published country feeds.
    for country in METEOALARM_COUNTRIES:
        url = (
            "https://feeds.meteoalarm.org/feeds/"
            f"meteoalarm-legacy-atom-{country}"
        )
        try:
            root = ET.fromstring(fetch(url))
            successful_feeds += 1
        except Exception as exc:
            feed_errors.append(f"{country}: {type(exc).__name__}")
            continue

        for entry in root.iter():
            if entry.tag.split("}")[-1] != "entry":
                continue

            title = child_text(entry, ["title"])
            entry_id = child_text(entry, ["id"]) or title
            summary = child_text(entry, ["summary", "content"])
            onset = child_text(entry, ["onset", "effective"])
            expires = child_text(entry, ["expires", "expiry"])
            area = child_text(entry, ["areaDesc", "area"])

            link = ""
            for child in entry:
                if child.tag.split("}")[-1] == "link":
                    href = child.attrib.get("href", "")
                    if href and child.attrib.get(
                        "rel", "alternate"
                    ) in ("alternate", ""):
                        link = href
                        break

            expiry_dt = parse_time(expires)
            if expiry_dt is not None and expiry_dt < current:
                continue

            alerts.append({
                "id": make_id("MeteoAlarm", entry_id or title),
                "source": "MeteoAlarm Europe",
                "event": available(title),
                "headline": available(summary),
                "description": available(summary),
                "area": available(area),
                "severity": available(
                    child_text(entry, ["severity", "awareness_level"])
                ),
                "urgency": available(child_text(entry, ["urgency"])),
                "certainty": available(child_text(entry, ["certainty"])),
                "start": onset or child_text(entry, ["updated", "published"]),
                "end": expires,
                "url": available(link) if link else NA,
                "kind": "weather",
            })

    if successful_feeds == 0:
        raise RuntimeError(
            "All MeteoAlarm country feeds failed: "
            + ", ".join(feed_errors[:8])
        )

    return alerts

def get_gdacs_events():
    root = ET.fromstring(fetch(GDACS_URL))
    events = []

    for item in root.iter():
        if item.tag.split("}")[-1] != "item":
            continue

        title = child_text(item, ["title"])
        link = child_text(item, ["link"])
        description = child_text(item, ["description"])
        published = child_text(item, ["pubDate"])
        guid = child_text(item, ["guid"]) or link or title

        events.append({
            "id": make_id("GDACS", guid),
            "source": "GDACS global disaster monitoring",
            "event": available(title),
            "headline": available(description),
            "description": available(description),
            "area": NA,
            "severity": NA,
            "urgency": NA,
            "certainty": NA,
            "start": published,
            "end": None,
            "url": available(link) if link else NA,
            "kind": "disaster",
        })

    return events


def format_alert(alert):
    details = available(
        alert.get("headline")
        if alert.get("headline") != NA
        else alert.get("description")
    )

    if details != NA:
        details = " ".join(details.split())
        if len(details) > 600:
            details = details[:597] + "..."

    return (
        f"🚨 **{available(alert.get('event'))}**\n"
        f"**Source:** {available(alert.get('source'))}\n"
        f"**Location:** {available(alert.get('area'))}\n"
        f"**Severity:** {available(alert.get('severity'))}\n"
        f"**Urgency:** {available(alert.get('urgency'))}\n"
        f"**Certainty:** {available(alert.get('certainty'))}\n"
        f"**Start / issued:** {display_time(alert.get('start'))}\n"
        f"**End / expires:** {display_time(alert.get('end'))}\n"
        f"**Details:** {details}\n"
        f"**Official alert data (JSON):** {available(alert.get('url'))}\n"
        + (
            f"**Readable NWS site:** {available(alert.get('human_url'))}\n"
            if alert.get("source") == "U.S. National Weather Service"
            else ""
        )
        + f"**Bot check time:** {display_time(now_utc())}"
    )


def send_hourly_fake_test(state):
    now = now_utc()
    hour_key = now.strftime("%Y-%m-%d-%H")

    if state.get("last_hourly_test") == hour_key:
        return

    fake_end = now.replace(minute=59, second=59, microsecond=0)

    message = (
        "🧪 **FAKE TEST EMERGENCY — NOT REAL — DO NOT ACT ON THIS**\n"
        f"**Fictional category:** {random.choice(FAKE_CATEGORIES)}\n"
        f"**Fictional location:** {random.choice(FAKE_CITIES)}\n"
        f"**Fake start:** {display_time(now)}\n"
        f"**Fake end:** {display_time(fake_end)}\n"
        "This is a randomized bot test, not an official alert or real emergency.\n"
        f"**Official tracking link:** {NA} (fictional event)\n"
        f"**Test sent:** {display_time(now)}"
    )

    post_discord(message)
    state["last_hourly_test"] = hour_key


def main():
    if not WEBHOOK:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not configured.")

    state = load_state()
    now = now_utc()

    # Only announce a startup for a manual workflow run.
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        post_discord(
            "🟢 **Team Vortex Weather Bot has been manually started!**\n"
            f"**Time:** {display_time(now)}"
        )

    successful_weather_feeds = []
    failed_feeds = []
    active_weather = []
    disaster_events = []

    for name, getter, category in [
        ("U.S. NWS", get_nws_alerts, "weather"),
        ("MeteoAlarm Europe", get_europe_alerts, "weather"),
        ("GDACS global disasters", get_gdacs_events, "disaster"),
    ]:
        try:
            items = getter()
            if category == "weather":
                successful_weather_feeds.append(name)
                active_weather.extend(items)
            else:
                disaster_events.extend(items)
        except Exception as exc:
            failed_feeds.append(
                f"{name}: {type(exc).__name__}: {str(exc)[:180]}"
            )

    all_items = active_weather + disaster_events
    seen = set(state["seen"])

    # On the first successful run, establish a baseline without posting
    # every historical alert into Discord.
    if not state["baseline_saved"]:
        state["seen"] = list(dict.fromkeys(
            item["id"] for item in all_items
        ))[-MAX_SEEN:]
        state["baseline_saved"] = True
        save_state(state)
        post_discord(
            "🧭 **Initial alert history created.**\n"
            "Existing feed entries were recorded silently to prevent a "
            "flood of old alerts. Newly detected alerts will be posted "
            "on later checks.\n"
            f"**Weather feeds working:** "
            f"{', '.join(successful_weather_feeds) or NA}\n"
            f"**Time:** {display_time(now_utc())}"
        )
    else:
        # A manual run doubles as a current-alert listing: show up to ten
        # active entries even if they were already recorded in history.
        # Scheduled checks only post newly discovered entries.
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            selected = all_items[:10]
        else:
            selected = [item for item in all_items if item["id"] not in seen][:10]

        # Mark all discovered entries seen, including entries beyond the cap,
        # so a large feed backlog never floods Discord.
        for item in all_items:
            if item["id"] not in seen:
                state["seen"].append(item["id"])
                seen.add(item["id"])
        state["seen"] = list(dict.fromkeys(state["seen"]))[-MAX_SEEN:]
        save_state(state)

        for item in selected:
            post_discord(format_alert(item))

    # Keep status summaries to one concise message per check.
    if len(successful_weather_feeds) == 2:
        if active_weather:
            status = (
                "📋 **Active weather alerts listed in monitored feeds**\n"
                f"**Count:** {len(active_weather)}\n"
                f"**Sources checked:** {', '.join(successful_weather_feeds)}"
            )
        else:
            status = (
                "✅ **No active weather alerts were listed in the monitored "
                "feeds at this check.**\n"
                f"**Sources checked:** {', '.join(successful_weather_feeds)}\n"
                "This does not guarantee that no alerts exist worldwide."
            )
    else:
        status = (
            "⚠️ **Active-alert status could not be fully confirmed.**\n"
            f"**Weather feeds that worked:** "
            f"{', '.join(successful_weather_feeds) or NA}\n"
            "At least one monitored weather feed failed; a full all-clear "
            "would be unreliable."
        )

    if failed_feeds:
        status += "\n**Sources with errors:** " + "; ".join(failed_feeds[:3])
    status += f"\n**Checked:** {display_time(now_utc())}"
    post_discord(status)

    try:
        send_hourly_fake_test(state)
    except Exception as exc:
        print(f"Hourly fake test could not be sent: {exc}")

    save_state(state)
    print(f"Check finished: {now.isoformat()}")
    print(f"Weather feeds working: {successful_weather_feeds}")
    print(f"New entries found: {len(all_items)}")
    if failed_feeds:
        print("Feed errors: " + " | ".join(failed_feeds))


if __name__ == "__main__":
    main()
