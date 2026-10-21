#!/usr/bin/env python3
"""Energex outage watcher -> Telegram alerts.

Polls Energex's public outage GeoJSON, keeps outages that touch your area
(a lat/lon box), and sends a Telegram message when one appears or clears.

Env vars:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (required unless --stdout)
  AREA_NAME       label used in messages        (default "Wynnum/Manly")
  AREA_BBOX       min_lon,min_lat,max_lon,max_lat (default covers Wynnum/Manly/Lota)
  INCLUDE_PLANNED set to 1 to also alert on planned outages
  STATE_FILE      where seen outages are remembered (default state.json)

Flags:
  --test    send one test message to Telegram and exit
  --stdout  print messages instead of sending (still updates state file)
"""
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Australia/Brisbane")
BASE = "https://www.energex.com.au/static/Energex/energex_po_{}.geojson"
MIRROR = "https://raw.githubusercontent.com/joelkoen/energyq-outages/main/energex_po_{}.geojson"
KINDS = ["current_unplanned"]
if os.environ.get("INCLUDE_PLANNED") == "1":
    KINDS.append("current_planned")

AREA_NAME = os.environ.get("AREA_NAME", "Wynnum/Manly")
BBOX = [float(x) for x in os.environ.get("AREA_BBOX", "153.12,-27.50,153.23,-27.40").split(",")]
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
UA = "Mozilla/5.0 (compatible; home-outage-monitor/1.0)"


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch(kind):
    """Official Energex feed first, community mirror as fallback."""
    local = os.environ.get("LOCAL_FEED")  # for testing only
    if local:
        with open(local) as f:
            return json.load(f)
    last = None
    for url in (BASE.format(kind), MIRROR.format(kind)):
        try:
            data = get_json(url)
            if data.get("type") == "FeatureCollection":
                return data
            last = f"unexpected payload from {url}"
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"fetch failed {url}: {e}", file=sys.stderr)
    raise RuntimeError(f"all sources failed for {kind}: {last}")


def coords(o):
    """Yield (lon, lat) pairs from any GeoJSON coordinates nesting."""
    if isinstance(o, (list, tuple)):
        if len(o) >= 2 and all(isinstance(v, (int, float)) for v in o[:2]):
            if abs(o[0]) <= 180 and abs(o[1]) <= 90:
                yield o[0], o[1]
        else:
            for item in o:
                yield from coords(item)


def in_area(feature):
    pts = list(coords((feature.get("geometry") or {}).get("coordinates")))
    if not pts:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return not (max(xs) < BBOX[0] or min(xs) > BBOX[2] or max(ys) < BBOX[1] or min(ys) > BBOX[3])


def event_key(feature):
    props = feature.get("properties") or {}
    if props.get("EVENT_ID") not in (None, ""):
        return str(props["EVENT_ID"])
    blob = json.dumps(feature, sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


def describe(props, limit=10):
    lines = []
    for k, v in props.items():
        if v in (None, "") or isinstance(v, (dict, list)):
            continue
        lines.append(f"{k}: {v}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def send(text, to_stdout=False):
    if to_stdout:
        print("---- message ----")
        print(text)
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200:
            raise RuntimeError(f"Telegram returned {r.status}")


def minutes_between(a_iso, b):
    return int((b - datetime.fromisoformat(a_iso)).total_seconds() // 60)


def main():
    to_stdout = "--stdout" in sys.argv
    if "--test" in sys.argv:
        send("⚡ Test alert from the outage monitor", to_stdout)
        return

    current, total_features = {}, 0
    for kind in KINDS:
        feats = fetch(kind).get("features", [])
        total_features += len(feats)
        for f in feats:
            if in_area(f):
                current[f"{kind}:{event_key(f)}"] = {
                    "kind": kind,
                    "text": describe(f.get("properties") or {}),
                }

    state = load_state()
    now = datetime.now(TZ)
    stamp = now.strftime("%a %d %b %H:%M")

    # New outages in the area
    for key, item in current.items():
        if key in state:
            continue
        label = "Unplanned outage" if item["kind"].endswith("unplanned") else "Planned outage"
        msg = f"⚡ {label} near {AREA_NAME}\nSeen: {stamp}\n{item['text']}"
        try:
            send(msg, to_stdout)
            state[key] = {"first_seen": now.isoformat(), **item}
        except Exception as e:  # noqa: BLE001
            print(f"alert failed for {key}: {e}", file=sys.stderr)  # retry next run

    # Cleared outages. Skip if the whole feed came back empty (likely a glitch).
    if total_features > 0:
        for key in [k for k in state if k not in current]:
            old = state[key]
            mins = minutes_between(old["first_seen"], now)
            msg = f"✅ Outage cleared near {AREA_NAME}\nCleared: {stamp}\nLasted about {mins} min\n{old['text']}"
            try:
                send(msg, to_stdout)
                del state[key]
            except Exception as e:  # noqa: BLE001
                print(f"clear alert failed for {key}: {e}", file=sys.stderr)
    else:
        print("feed empty, skipping clear check", file=sys.stderr)

    save_state(state)
    print(f"{stamp}: {len(current)} outage(s) in area, {total_features} network-wide")


if __name__ == "__main__":
    main()
