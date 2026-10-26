#!/usr/bin/env python3
"""Generate a weekly TEEP PNG and a small GitHub Pages publication.

Credentials come only from environment variables. Published output contains
event titles, times, types and participating elements; never briefs or keys.
"""
import html
import io
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

TZ = ZoneInfo("America/Chicago")
SITE = "https://suvzert.github.io/teep-calendar/"
OUT = Path("public")
# Display palette, independent of Teamup's numeric color identifiers.
ELEMENTS = {
    15058025: ("ACE", "#8951C7"),
    15058007: ("CE", "#718096"),
    15058008: ("GCE / 1st Platoon", "#278453"),
    15058102: ("LCE / STP", "#EA7B30"),
    15058028: ("MEU-Wide", "#E8BD34"),
}
TYPES = {
    "admin": "Administration", "administration": "Administration",
    "training": "Training", "exercise": "Exercise",
    "operation": "Operation", "evaluation": "Evaluation",
}


def get_bytes(url, headers=None, limit=25_000_000):
    request = Request(url, headers={"User-Agent": "TEEP-Calendar/1.0", **(headers or {})})
    with urlopen(request, timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Response exceeded the size limit.")
    return data


def api(resource, params=None):
    key = os.environ.get("TEAMUP_CALENDAR_KEY", "").strip()
    token = os.environ.get("TEAMUP_API_KEY", "").strip()
    if not re.fullmatch(r"ks[A-Za-z0-9]+", key) or not token:
        raise ValueError("Missing or invalid Teamup secrets.")
    url = f"https://api.teamup.com/{key}/{resource}"
    if params:
        url += "?" + urlencode(params)
    return json.loads(get_bytes(url, {"Teamup-Token": token, "Accept": "application/json"}))


def choose_week(now, override=""):
    if override:
        start = date.fromisoformat(override)
        if start.weekday() != 0:
            raise ValueError("WEEK_START must be a Monday in YYYY-MM-DD format.")
        return start
    today = now.astimezone(TZ).date()
    # Sunday publishes the upcoming week; Monday-Saturday use the active week.
    return today + timedelta(days=1) if today.weekday() == 6 else today - timedelta(days=today.weekday())


def parse_dt(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=TZ)
    return result.astimezone(TZ)


def text(value):
    return " ".join(str(value or "").split())


def prepare_days(events, calendars, monday):
    names = {int(c["id"]): text(c["name"]) for c in calendars}
    days = [[] for _ in range(7)]
    seen = set()
    for event in events:
        identity = (str(event["id"]), event["start_dt"])
        if identity in seen:
            continue
        seen.add(identity)
        start, end = parse_dt(event["start_dt"]), parse_dt(event["end_dt"])
        if end < start:
            raise ValueError("An event ends before it starts.")
        ids = event.get("subcalendar_ids") or [event.get("subcalendar_id")]
        ids = list(dict.fromkeys(int(i) for i in ids if i is not None))
        labels = [ELEMENTS[i][0] if i in ELEMENTS else names.get(i, "Other") for i in ids]
        types = (event.get("custom") or {}).get("event_type") or []
        if isinstance(types, str):
            types = [types]
        kind = " / ".join(TYPES.get(str(t), str(t).replace("_", " ").title()) for t in types)
        color = ELEMENTS.get(ids[0], ("", "#718096"))[1] if len(ids) == 1 else "#E8BD34"
        for index in range(7):
            day = monday + timedelta(days=index)
            lo = datetime.combine(day, time.min, TZ)
            hi = datetime.combine(day + timedelta(days=1), time.min, TZ)
            if event.get("all_day"):
                # Teamup all-day dates represent calendar dates, not UTC instants.
                first = date.fromisoformat(event["start_dt"][:10])
                last = date.fromisoformat(event["end_dt"][:10])
                if not first <= day <= last:
                    continue
                when = "ALL DAY"
            else:
                if not (start < hi and end > lo) and not (start == end and lo <= start < hi):
                    continue
                a = start.strftime("%H:%M") if start >= lo else "00:00"
                b = end.strftime("%H:%M") if end < hi else "24:00"
                when = f"{a} - {b}"
            days[index].append({
                "title": text(event.get("title")) or "Untitled event",
                "when": when, "detail": "  |  ".join(filter(None, [kind, ", ".join(labels)])),
                "color": color, "sort": (not event.get("all_day", False), start, text(event.get("title"))),
            })
    for day in days:
        day.sort(key=lambda item: item["sort"])
    return days


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(name, size)


def wrap(value, face, width):
    # Character wrapping also handles unusually long unbroken event names.
    lines, line = [], ""
    for char in value:
        if line and face.getlength(line + char) > width:
            split = line.rfind(" ")
            if split > 0:
                lines.append(line[:split])
                line = line[split + 1:] + char
            else:
                lines.append(line)
                line = char
        else:
            line += char
    return lines + [line.strip()]


def draw_calendar(days, monday, now, destination):
    heading, title, body, small = font(48, True), font(29, True), font(23), font(20)
    layout = []
    for day in days:
        cards = []
        for event in day:
            titles = wrap(event["title"], title, 1020)
            details = wrap(event["detail"], small, 1020)
            height = 62 + len(titles) * 36 + len(details) * 27
            cards.append((event, titles, details, height))
        layout.append((cards, max(100, sum(c[3] + 12 for c in cards) + 12)))
    height = 255 + sum(h + 12 for _, h in layout) + 80
    if height > 24000:
        raise ValueError("Too many events for one readable image; split the schedule before publishing.")
    image = Image.new("RGB", (1400, height), "#101722")
    draw = ImageDraw.Draw(image)
    draw.text((38, 28), "11TH MEU (SOC)", fill="#E8BD34", font=body)
    draw.text((38, 65), "WEEKLY TEEP", fill="white", font=heading)
    sunday = monday + timedelta(days=6)
    draw.text((38, 130), f"{monday:%d %b %Y} - {sunday:%d %b %Y}  |  CENTRAL TIME", fill="#C4CDD9", font=body)
    draw.text((38, 173), "24-hour times  /  Event details and RSVPs remain in Apollo", fill="#94A3B8", font=small)
    y = 225
    for index, (cards, row_height) in enumerate(layout):
        day = monday + timedelta(days=index)
        draw.rounded_rectangle((28, y, 1372, y + row_height), radius=12, fill="#1B2636")
        draw.text((48, y + 20), day.strftime("%a").upper(), font=title, fill="#E8BD34")
        draw.text((48, y + 59), day.strftime("%d %b"), font=body, fill="white")
        cy = y + 12
        if not cards:
            draw.text((245, cy + 22), "No scheduled events", font=body, fill="#94A3B8")
        for event, titles, details, ch in cards:
            draw.rounded_rectangle((224, cy, 1358, cy + ch), radius=8, fill="#243247")
            draw.rectangle((224, cy + 8, 231, cy + ch - 8), fill=event["color"])
            draw.text((248, cy + 12), event["when"], font=body, fill="#E8BD34")
            ty = cy + 46
            for line in titles:
                draw.text((248, ty), line, font=title, fill="white")
                ty += 36
            for line in details:
                draw.text((248, ty + 5), line, font=small, fill="#BFCCDC")
                ty += 27
            cy += ch + 12
        y += row_height + 12
    draw.text((38, y + 14), f"Updated {now:%d %b %Y at %H:%M %Z}  |  Source: Teamup", font=small, fill="#94A3B8")
    image.save(destination, "PNG", optimize=True)


def preserve_archive():
    # Pages replaces its entire publication on deploy. Copy prior images first.
    try:
        archive = json.loads(get_bytes(SITE + "archive.json", {"Cache-Control": "no-cache"}))
    except HTTPError as error:
        if error.code == 404:
            return []  # First publication.
        raise
    if not isinstance(archive, list):
        raise ValueError("Invalid published archive; refusing to overwrite it.")
    for item in archive:
        name = item["image"]
        if not re.fullmatch(r"teep-\d{4}-\d{2}-\d{2}-[a-zA-Z0-9-]+\.png", name):
            raise ValueError("Invalid archive filename.")
        data = get_bytes(SITE + name)
        with Image.open(io.BytesIO(data)) as prior:
            prior.verify()
        (OUT / name).write_bytes(data)
    return archive


def main():
    now = datetime.now(TZ)
    monday = choose_week(now, os.environ.get("WEEK_START", "").strip())
    sunday = monday + timedelta(days=6)
    events = api("events", {"startDate": monday.isoformat(), "endDate": sunday.isoformat(), "tz": "America/Chicago"})["events"]
    calendars = api("subcalendars")["subcalendars"]
    days = prepare_days(events, calendars, monday)
    OUT.mkdir(exist_ok=True)
    archive = preserve_archive()
    build = os.environ.get("BUILD_ID", now.strftime("%Y%m%d%H%M%S"))
    if not re.fullmatch(r"[A-Za-z0-9-]+", build):
        raise ValueError("Invalid build identifier.")
    filename = f"teep-{monday.isoformat()}-{build}.png"
    draw_calendar(days, monday, now, OUT / filename)
    manifest = {
        "week_key": monday.isoformat(), "week_end": sunday.isoformat(),
        "image": filename, "image_url": SITE + filename,
        "request_id": os.environ.get("REQUEST_ID", ""), "build_id": build,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "event_count": len({(str(e["id"]), e["start_dt"]) for e in events}),
    }
    # The marker is published together with the image, so consumers can wait for it.
    (OUT / "latest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = [entry for entry in archive if entry["image"] != filename]
    archive.append({k: manifest[k] for k in ("week_key", "image", "generated_at")})
    (OUT / "archive.json").write_text(json.dumps(archive, indent=2), encoding="utf-8")
    links = "".join(f'<li><a href="{html.escape(e["image"])}">Week of {html.escape(e["week_key"])} — {html.escape(e["generated_at"])}</a></li>' for e in reversed(archive))
    page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>11th MEU (SOC) — Weekly TEEP</title>'
            '<style>body{background:#101722;color:#eee;font:16px system-ui;margin:24px}img{width:100%;max-width:1000px}a{color:#e8bd34}</style>'
            '<h1>11th MEU (SOC) — Weekly TEEP</h1>'
            f'<img src="{filename}" alt="Weekly TEEP calendar"><h2>Archive</h2><ul>{links}</ul></html>')
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / ".nojekyll").touch()
    print(f"Generated week {monday}: {manifest['event_count']} events.")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        # Do not print exception URLs: the Teamup URL contains a secret key.
        print(f"Request failed with HTTP {error.code}; publication stopped.", file=sys.stderr)
        sys.exit(1)
    except (URLError, TimeoutError):
        print("Network request failed; publication stopped. Retry the workflow.", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"Generation failed ({type(error).__name__}); publication stopped.", file=sys.stderr)
        sys.exit(1)
