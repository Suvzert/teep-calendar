#!/usr/bin/env python3
"""Generate a weekly TEEP PNG and a small GitHub Pages publication.

Credentials come only from environment variables. Published output contains
event titles, times, types and visible member-facing event details; never keys.
"""
import html
import io
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

TZ = ZoneInfo("America/Chicago")
SITE = "https://suvzert.github.io/teep-calendar/"
OUT = Path("public")
# Teamup's 48-color palette, verified from its official calendar client.
# The API supplies numeric color IDs; names and IDs are refreshed every run.
PALETTE = {1: '#f2665b', 2: '#cf2424', 3: '#a01a1a', 4: '#7e3838', 5: '#ca7609', 6: '#f16c20', 7: '#f58a4b', 8: '#d2b53b', 9: '#d96fbf', 10: '#b84e9d', 11: '#9d3283', 12: '#7a0f60', 13: '#542382', 14: '#7742a9', 15: '#8763ca', 16: '#b586e2', 18: '#4770d8', 19: '#2951b9', 20: '#133897', 21: '#1a5173', 22: '#1a699c', 23: '#0080a6', 24: '#4aaace', 25: '#88b347', 26: '#5a8121', 27: '#2d850e', 28: '#176413', 29: '#0f4c30', 30: '#386651', 31: '#00855b', 32: '#4fb5a1', 33: '#553711', 34: '#724f22', 35: '#9c6013', 36: '#f6c811', 37: '#ce1212', 38: '#b20d47', 39: '#d8135a', 40: '#e81f78', 41: '#f5699a', 42: '#5c1c1c', 43: '#a55757', 44: '#c37070', 45: '#000000', 46: '#383838', 47: '#757575', 48: '#a3a3a3', 17: '#668CB3'}


def teamup_color(value):
    if isinstance(value, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
        return "#" + value.lstrip("#")
    try:
        return PALETTE[int(value)]
    except (ValueError, TypeError, KeyError):
        raise ValueError("Unrecognized Teamup color; update the palette before publishing.")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if self.skip:
            return
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n" + ("• " if tag == "li" else ""))
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href.startswith(("https://", "http://")):
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in ("p", "div", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain(value):
    parser = PlainText()
    parser.feed(str(value or ""))
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    content = "\n".join(line for line in lines if line)
    for link in dict.fromkeys(parser.links):
        if link not in content:
            content += "\n" + link
    return content.strip()


def event_details(event, definitions):
    result = []
    for key, builtin, default in [("who", "builtin_who", "Event POC"),
                                  ("location", "builtin_location", "Event Location"),
                                  ("notes", "builtin_notes", "Event Brief")]:
        definition = definitions.get(builtin, {})
        if definition.get("hidden") or definition.get("active") is False:
            continue
        value = plain(event.get(key))
        if value.lower() not in ("", "none", "null", "n/a"):
            result.append((definition.get("name") or default, value))
    for key, value in (event.get("custom") or {}).items():
        if key == "event_type":
            continue
        definition = definitions.get(key, {})
        if definition.get("hidden") or definition.get("active") is False:
            continue
        if isinstance(value, list):
            options = {o["id"]: o.get("name", o["id"]) for o in definition.get("type_data", {}).get("options", [])}
            value = ", ".join(str(options.get(v, v)) for v in value)
        if plain(value):
            result.append((definition.get("name") or key, plain(value)))
    if not definitions.get("builtin_attachments", {}).get("hidden"):
        for attachment in event.get("attachments") or []:
            if isinstance(attachment, dict):
                name = attachment.get("name") or attachment.get("filename") or "Attachment"
                url = attachment.get("url") or attachment.get("link") or ""
                result.append(("Attachment", plain(name) + ("\n" + url if url.startswith("https://") else "")))
    return result


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


def prepare_days(events, calendars, monday, configuration=None):
    calendar_map = {int(c["id"]): c for c in calendars}
    definitions = {f["id"]: f for f in (configuration or {}).get("fields", {}).get("definitions", [])}
    options = {o["id"]: o for o in definitions.get("event_type", {}).get("type_data", {}).get("options", [])}
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
        labels = [text(calendar_map[i]["name"]) for i in ids if i in calendar_map]
        colors = [teamup_color(calendar_map[i]["color"]) for i in ids if i in calendar_map]
        types = (event.get("custom") or {}).get("event_type") or []
        if isinstance(types, str):
            types = [types]
        tags = [{"label": str(options.get(t, {}).get("name", t)).upper(),
                 "color": teamup_color(options[t]["color"]) if t in options else "#757575"} for t in types]
        details = event_details(event, definitions)
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
                "when": when, "calendars": labels, "colors": [t["color"] for t in tags] or ["#757575"],
                "tags": tags, "details": details, "sort": (not event.get("all_day", False), start, text(event.get("title"))),
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


def foreground(color):
    rgb = tuple(int(color.lstrip("#")[i:i+2], 16) / 255 for i in (0, 2, 4))
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    lum = sum(a*b for a, b in zip(linear, (0.2126, 0.7152, 0.0722)))
    return "#101722" if lum > 0.179 else "#FFFFFF"


def draw_calendar(days, monday, now, destination):
    heading, title, body, small = font(46, True), font(28, True), font(23), font(21)
    layout = []
    for day in days:
        cards = []
        for event in day:
            titles = wrap(event["title"], title, 1055)
            rows = []
            for label in event["calendars"]:
                rows.extend(wrap(label, small, 1015))
            for label, value in event["details"]:
                rows.append("")
                rows.extend(wrap(label.upper() + ":", small, 1055))
                for paragraph in value.splitlines():
                    rows.extend(wrap(paragraph, small, 1055))
            header_h = 54 + len(titles) * 35
            ch = header_h + (48 if event["tags"] else 8) + len(rows) * 28 + 22
            cards.append((event, titles, rows, header_h, ch))
        layout.append((cards, max(100, sum(c[4] + 14 for c in cards) + 14)))
    height = 225 + sum(h + 14 for _, h in layout) + 80
    if height > 24000:
        raise ValueError("Too many details for one image; reduce event briefs before publishing.")
    image = Image.new("RGB", (1400, height), "#101722")
    draw = ImageDraw.Draw(image)
    draw.text((38, 24), "11TH MEU (SOC)", fill="#E8BD34", font=body)
    draw.text((38, 60), "WEEKLY TEEP", fill="white", font=heading)
    sunday = monday + timedelta(days=6)
    draw.text((38, 124), f"{monday:%d %b %Y} - {sunday:%d %b %Y}  |  CENTRAL TIME", fill="#C4CDD9", font=body)
    draw.text((38, 165), "24-hour times  /  Calendar colors and event tags from Teamup", fill="#94A3B8", font=small)
    y = 215
    for index, (cards, row_height) in enumerate(layout):
        day = monday + timedelta(days=index)
        draw.rounded_rectangle((25, y, 1375, y + row_height), radius=12, fill="#1B2636")
        draw.text((45, y + 20), day.strftime("%a").upper(), font=title, fill="#E8BD34")
        draw.text((45, y + 59), day.strftime("%d %b"), font=body, fill="white")
        cy = y + 14
        if not cards:
            draw.text((245, cy + 22), "No scheduled events", font=body, fill="#94A3B8")
        for event, titles, rows, hh, ch in cards:
            x0, x1 = 210, 1360
            draw.rounded_rectangle((x0, cy, x1, cy + ch), radius=8, fill="#243247")
            colors = event["colors"]
            # Exact calendar hues; shared events retain every assigned color.
            if len(colors) == 1:
                draw.rectangle((x0, cy, x1, cy + hh), fill=colors[0])
                ink = foreground(colors[0])
            else:
                draw.rectangle((x0, cy, x1, cy + hh), fill="#172232")
                for i, color in enumerate(colors):
                    left = x0 + round((x1-x0)*i/len(colors))
                    right = x0 + round((x1-x0)*(i+1)/len(colors))
                    draw.rectangle((left, cy, right, cy + 10), fill=color)
                ink = "#FFFFFF"
            draw.text((234, cy + 16), event["when"], font=body, fill=ink)
            ty = cy + 49
            for line in titles:
                draw.text((234, ty), line, font=title, fill=ink)
                ty += 35
            ty = cy + hh + 10
            tx = 234
            for tag in event["tags"]:
                tw = int(small.getlength(tag["label"])) + 24
                draw.rounded_rectangle((tx, ty, tx+tw, ty+32), radius=5, fill=tag["color"])
                draw.text((tx+12, ty+3), tag["label"], font=small, fill=foreground(tag["color"]))
                tx += tw + 9
            if event["tags"]:
                ty += 38
            # All calendar names and fields wrap; content determines card height.
            calendar_line_count = sum(len(wrap(label, small, 1015)) for label in event["calendars"])
            line_index = 0
            for label, color in zip(event["calendars"], colors):
                lines = wrap(label, small, 1015)
                draw.rectangle((235, ty+6, 246, ty+17), fill=color)
                for line in lines:
                    draw.text((259, ty), line, font=small, fill="#DBE4EF")
                    ty += 28
                    line_index += 1
            for line in rows[calendar_line_count:]:
                draw.text((234, ty), line, font=small, fill="#DBE4EF")
                ty += 28
            cy += ch + 14
        y += row_height + 14
    draw.text((38, y + 14), f"Updated {now:%d %b %Y at %H:%M %Z}  |  Source: Teamup  |  RSVP in Apollo", font=small, fill="#94A3B8")
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
        snapshot = name.replace('.png', '.discord.json')
        try:
            (OUT / snapshot).write_bytes(get_bytes(SITE + snapshot))
        except HTTPError as error:
            if error.code != 404:
                raise
    return archive


# Discord Components V2 output. These payloads describe messages; BotGhost must
# attach actions to the custom_id controls before they are posted to members.
def discord_bundle(events, calendars, monday, configuration, now):
    import hashlib
    definitions = {f['id']: f for f in configuration.get('fields', {}).get('definitions', [])}
    choices = {o['id']: o for o in definitions.get('event_type', {}).get('type_data', {}).get('options', [])}
    units = {str(c['id']): c['name'] for c in calendars}
    week = monday.isoformat()
    records = []
    seen = set()
    priority = ['OPS', 'FTX', 'TRNG', 'EVAL', 'ADMIN']
    def safe(s):
        # Preserve URLs and line breaks, prevent mentions and formatting injection.
        return str(s).replace('@', '@\u200b').replace('`', '\\`').replace('*', '\\*').replace('_', '\\_')
    def clip(s, n):
        return s if len(s) <= n else s[:n-1] + '…'
    def box(s, color):
        return {'type': 17, 'accent_color': color, 'components': [{'type': 10, 'content': s}]}
    def message(parts, private=False):
        return {'flags': 32768 | (64 if private else 0), 'allowed_mentions': {'parse': []}, 'components': parts}
    def menu(cid, placeholder, options):
        return {'type': 1, 'components': [{'type': 3, 'custom_id': cid, 'placeholder': placeholder,
                'min_values': 1, 'max_values': 1, 'options': options}]}
    for e in sorted(events, key=lambda e: (e['start_dt'], str(e['id']))):
        identity = str(e['id']) + ':' + e['start_dt']
        if identity in seen:
            continue
        seen.add(identity)
        # Filter to the selected week using the same rules as the PNG.
        if not any(prepare_days([e], calendars, monday, configuration)):
            continue
        key = hashlib.sha256(identity.encode()).hexdigest()[:16]
        types = (e.get('custom') or {}).get('event_type') or []
        if isinstance(types, str):
            types = [types]
        tags = [str(choices.get(t, {}).get('name', t)).upper() for t in types]
        primary = min(tags, key=lambda t: priority.index(t) if t in priority else 99) if tags else 'EVENT'
        primary_id = next((t for t in types if str(choices.get(t, {}).get('name', t)).upper() == primary), None)
        color = int(teamup_color(choices.get(primary_id, {}).get('color', 47))[1:], 16)
        start, end = parse_dt(e['start_dt']), parse_dt(e['end_dt'])
        if e.get('all_day'):
            when = e['start_dt'][:10] + ' • ALL DAY'
            if e['start_dt'][:10] != e['end_dt'][:10]:
                when += ' through ' + e['end_dt'][:10]
        else:
            when = f"{start:%a %b %d, %H:%M %Z} – {end:%a %b %d, %H:%M %Z}"
        ids = list(dict.fromkeys(str(i) for i in (e.get('subcalendar_ids') or [e.get('subcalendar_id')]) if i is not None))
        title = plain(e.get('title')) or 'Untitled event'
        details = event_details(e, definitions)
        records.append({'key': key, 'title': title, 'tags': tags, 'color': color, 'when': when,
                        'unit_ids': ids, 'units': [units[i] for i in ids if i in units],
                        'details': [{'name': k, 'value': v} for k, v in details]})
    bundle = {'schema_version': 1, 'week_key': week, 'timezone': 'America/Chicago',
              'generated_at': now.isoformat(), 'events': records, 'views': {}, 'routes': {},
              'integration_status': 'Connect controls to BotGhost actions before sending.'}
    prefix = f'teep:{week}'
    # Every detail is split into pages; no brief is silently discarded.
    for e in records:
        heading = f"## {safe(e['title'])}\n**{' / '.join(e['tags']) or 'EVENT'}**\n{e['when']}\n{safe(' • '.join(e['units']))}"
        body = heading + ''.join(f"\n\n**{safe(d['name'])}**\n{safe(d['value'])}" for d in e['details'])
        chunks = [body[i:i+3300] for i in range(0, len(body), 3300)]
        payloads = []
        for i, chunk in enumerate(chunks):
            parts = [box(chunk, e['color'])]
            if len(chunks) > 1:
                buttons = [{'type': 2, 'style': 2, 'label': label,
                            'custom_id': f"{prefix}:detail:{e['key']}:{target}"}
                           for label, target in [('Previous', i-1), ('Next', i+1)] if 0 <= target < len(chunks)]
                parts.append({'type': 1, 'components': buttons})
            payloads.append(message(parts, True))
            bundle['routes'][f"{prefix}:detail:{e['key']}:{i}"] = {'kind': 'detail', 'event_key': e['key'], 'page': i, 'ephemeral': True}
        e['messages'] = payloads
    # Seven event cards per page leaves room for menus and navigation below 40 components.
    filters = {'all': records, **{uid: [e for e in records if uid in e['unit_ids']] for uid in units}}
    for filter_id, selected in filters.items():
        pages = [selected[i:i+7] for i in range(0, len(selected), 7)] or [[]]
        payloads = []
        for index, page in enumerate(pages):
            header = f"# 11th MEU (SOC) — WEEKLY TEEP\n{week} – {(monday + timedelta(days=6)).isoformat()}\nAmerica/Chicago • Page {index+1}/{len(pages)}"
            parts = [{'type': 10, 'content': header}]
            for e in page:
                content = f"### {clip(safe(e['title']), 180)}\n**{' / '.join(e['tags']) or 'EVENT'}** • {e['when']}\n{clip(safe(' • '.join(e['units'])), 160)}"
                parts.append(box(content, e['color']))
            if not page:
                parts.append(box('No scheduled events for this selection.', 0x757575))
            if page:
                parts.append(menu(f'{prefix}:event', 'Choose an event for full details', [
                    {'label': clip(e['title'], 100), 'value': e['key'], 'description': clip(e['when'], 100)} for e in page]))
            # Guard the unit selector against Discord's 25-option limit.
            unit_options = [{'label': 'All elements', 'value': 'all'}] + [{'label': clip(name, 100), 'value': uid} for uid, name in units.items()]
            # Current source has five units. Fail rather than publish invalid controls.
            if len(unit_options) > 25:
                raise ValueError('More than 24 unit filters; split the unit selector before publishing.')
            parts.append(menu(f'{prefix}:unit', 'Filter by element (private view)', unit_options))
            buttons = [{'type': 2, 'style': 2, 'label': label,
                        'custom_id': f'{prefix}:view:{filter_id}:{target}'}
                       for label, target in [('Previous', index-1), ('Next', index+1)] if 0 <= target < len(pages)]
            buttons.append({'type': 2, 'style': 5, 'label': 'Calendar & archive', 'url': SITE})
            parts.append({'type': 1, 'components': buttons})
            payloads.append(message(parts, filter_id != 'all'))
            bundle['routes'][f'{prefix}:view:{filter_id}:{index}'] = {'kind': 'view', 'filter': filter_id, 'page': index, 'ephemeral': True}
        bundle['views'][filter_id] = payloads
    bundle['routes'][f'{prefix}:event'] = {'kind': 'event_selection', 'value': 'event key', 'ephemeral': True}
    bundle['routes'][f'{prefix}:unit'] = {'kind': 'unit_selection', 'value': 'unit id or all', 'ephemeral': True}
    # Validate limits before any output is written.
    def walk(parts):
        for part in parts:
            yield part
            yield from walk(part.get('components', []))
    for payload in [p for pages in bundle['views'].values() for p in pages] + [p for e in records for p in e['messages']]:
        components = list(walk(payload['components']))
        if len(components) > 40 or sum(len(c.get('content', '')) for c in components) > 4000:
            raise ValueError('Discord component or text limit exceeded.')
    return bundle


def write_discord(events, calendars, monday, configuration, now, filename):
    bundle = discord_bundle(events, calendars, monday, configuration, now)
    for name, data in [('discord-data.json', bundle),
                       (filename.replace('.png', '.discord.json'), bundle),
                       ('discord.json', bundle['views']['all'][0])]:
        (OUT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def main():
    now = datetime.now(TZ)
    monday = choose_week(now, os.environ.get("WEEK_START", "").strip())
    sunday = monday + timedelta(days=6)
    events = api("events", {"startDate": monday.isoformat(), "endDate": sunday.isoformat(), "tz": "America/Chicago"})["events"]
    calendars = api("subcalendars")["subcalendars"]
    configuration = api("configuration")["configuration"]
    days = prepare_days(events, calendars, monday, configuration)
    OUT.mkdir(exist_ok=True)
    archive = preserve_archive()
    build = os.environ.get("BUILD_ID", now.strftime("%Y%m%d%H%M%S"))
    if not re.fullmatch(r"[A-Za-z0-9-]+", build):
        raise ValueError("Invalid build identifier.")
    filename = f"teep-{monday.isoformat()}-{build}.png"
    draw_calendar(days, monday, now, OUT / filename)
    write_discord(events, calendars, monday, configuration, now, filename)
    manifest = {
        "week_key": monday.isoformat(), "week_end": sunday.isoformat(),
        "image": filename, "image_url": SITE + filename,
        "discord_url": SITE + "discord.json", "discord_data_url": SITE + "discord-data.json",
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
