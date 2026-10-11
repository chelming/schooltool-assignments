"""Scrape SchoolTool on an interval and publish per-student sensors to HA via MQTT."""
import datetime as dt
import json
import os
import re
import time
import zoneinfo

import paho.mqtt.client as mqtt

from schooltool import SchoolTool, SchoolToolError, classify, low_grades, period_at

E = os.environ
INTERVAL = int(E.get("INTERVAL_MINUTES", "60")) * 60
RETRY = 300          # after a failed scrape, try again sooner than the next bell
BELL_SLACK = dt.timedelta(seconds=15)
# Period start times, 24h, comma-separated: BELL_TIMES="07:45,08:35,...". Each one
# triggers a re-scrape so the sensors flip as classes change; add an end-of-day time
# (e.g. 15:00) so they flip to "No class" after the last period. Unset means no
# bell-time scrapes, only the INTERVAL_MINUTES poll and the nightly run.
BELL_TIMES = E.get("BELL_TIMES", "")
BELLS_TODAY = sorted({dt.time.fromisoformat(x.strip()) for x in BELL_TIMES.split(",") if x.strip()})
TZ = zoneinfo.ZoneInfo(E.get("TZ", "America/New_York"))
NIGHTLY = dt.time(1, 0)
MAX_ITEMS = 30
LOW_GRADE_PCT = float(E.get("LOW_GRADE_PCT", "75"))
PREFIX = E.get("TOPIC_PREFIX", "schooltool")
DISC = E.get("DISCOVERY_PREFIX", "homeassistant")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def item(r):
    return {"course": re.sub(r"\s*\(.*\)$", "", r["course"]).title(),
            "name": r["name"], "due": r["due"].isoformat(),
            "category": r["category"], "points": r["max"],
            **({"score": r["score"], "percent": r["pct"]} if "pct" in r else {})}


def line(r):
    """'ENGLISH 9 (Smith)' + name + due -> 'ENGLISH 9 - Smith - Membean #6 ... - 11/7/2026'."""
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", r["course"])
    course = f"{m.group(1)} - {m.group(2)}" if m else r["course"]
    d = r["due"]
    grade = f" ({r['score']:g}/{r['max']:g}, {r['pct']:g}%)" if "pct" in r else ""
    return f"{course} - {r['name']} - {d.month}/{d.day}/{d.year}{grade}"


def publish_current(mq, sid, dev, current):
    base = f"{PREFIX}/{sid}/current_class"
    mq.publish(f"{DISC}/sensor/schooltool_{sid}_current_class/config", json.dumps({
        "name": "Current class", "unique_id": f"schooltool_{sid}_current_class",
        "state_topic": f"{base}/state", "json_attributes_topic": f"{base}/attrs",
        "availability_topic": f"{PREFIX}/status", "icon": "mdi:school",
        "device": dev}), retain=True)
    if current:
        # class is None for a free period: the slot comes from the bell schedule
        state = f"{current['period']} ({current['times']}) {current['class'] or 'No class'}"
    else:
        state = "No class"
    mq.publish(f"{base}/state", state, retain=True)
    mq.publish(f"{base}/attrs", json.dumps({
        **(current or {}),
        "updated": dt.datetime.now(TZ).isoformat(timespec="seconds")}), retain=True)


def publish(mq, student, upcoming, overdue, low, current):
    sid = slug(student["name"])
    dev = {"identifiers": [f"schooltool_{sid}"], "name": f"SchoolTool {student['name']}",
           "manufacturer": "SchoolTool"}
    publish_current(mq, sid, dev, current)
    for kind, rows, label, icon in (
            ("upcoming", upcoming, "Upcoming assignments", "mdi:clipboard-text-clock"),
            ("overdue", overdue, "Overdue or ungraded assignments", "mdi:clipboard-alert"),
            ("low_grades", low, f"Graded {LOW_GRADE_PCT:g}% or lower", "mdi:school-outline")):
        base = f"{PREFIX}/{sid}/{kind}"
        mq.publish(f"{DISC}/sensor/schooltool_{sid}_{kind}/config", json.dumps({
            "name": label, "unique_id": f"schooltool_{sid}_{kind}",
            "state_topic": f"{base}/state", "json_attributes_topic": f"{base}/attrs",
            "availability_topic": f"{PREFIX}/status", "unit_of_measurement": "assignments",
            "icon": icon, "device": dev}), retain=True)
        mq.publish(f"{base}/state", len(rows), retain=True)
        mq.publish(f"{base}/attrs", json.dumps({
            "lines": [line(r) for r in rows[:MAX_ITEMS]],
            "items": [item(r) for r in rows[:MAX_ITEMS]],
            "next_due": rows[0]["due"].isoformat() if rows and kind == "upcoming" else None,
            "updated": dt.datetime.now(TZ).isoformat(timespec="seconds")}), retain=True)


BELLS = {}   # student id -> (date, bell schedule); bell times are fixed, fetch once a day


def free_slot(st, student, now):
    """Period the student is in when the header shows no class (free period).

    The header is empty when nothing meets, so the period comes from the Schedule
    tab's grid. Weekdays only; holidays still read as a free slot.
    """
    if now.weekday() >= 5:
        return None
    day, bell = BELLS.get(student["id"], (None, None))
    if day != now.date():
        try:
            bell = st.bell_schedule()
        except Exception as e:
            print(f"{student['name']}: bell schedule failed: {type(e).__name__}: {e}",
                  flush=True)
            return None
        BELLS[student["id"]] = (now.date(), bell)
    slot = period_at(bell, now.hour * 60 + now.minute)
    return {"period": slot["period"], "times": slot["times"], "class": None} if slot else None


def next_nightly(now):
    """The next 01:00 local, every day including weekends, so 'upcoming' rolls over."""
    t = dt.datetime.combine(now.date(), NIGHTLY, TZ)
    return t if t > now else t + dt.timedelta(days=1)


def next_sleep():
    """Seconds until the next bell (periods start at fixed times), capped at INTERVAL.

    Bell and hourly wake-ups are weekdays only; the nightly 01:00 run happens every
    day so due dates that passed over a weekend or holiday drop out of "upcoming".
    """
    now = dt.datetime.now(TZ)
    nightly = next_nightly(now)
    wake = min(now + dt.timedelta(seconds=INTERVAL), nightly)
    if now.weekday() < 5:
        wake = min([wake] + [t for t in (dt.datetime.combine(now.date(), b, TZ) + BELL_SLACK
                                         for b in BELLS_TODAY) if t > now])
    # a wake-up that would land on Sat/Sun is skipped in favour of the nightly run
    if wake.weekday() >= 5:
        wake = nightly
    return (wake - now).total_seconds()


def run_once(mq):
    st = SchoolTool(E["SCHOOLTOOL_URL"], E["SCHOOLTOOL_USERNAME"], E["SCHOOLTOOL_PASSWORD"])
    st.login()
    today = dt.datetime.now(TZ).date()
    for student in st.students():
        rows, current = st.assignments(student)
        if current is None and not st.last_header:
            current = free_slot(st, student, dt.datetime.now(TZ))
        up, over = classify(rows, today)
        low = low_grades(rows, LOW_GRADE_PCT / 100)
        publish(mq, student, up, over, low, current)
        if current is None and st.last_header:
            # an empty header just means no class is in session; text we failed to
            # parse means the wording changed, so log it
            print(f"{student['name']}: unparsed header={st.last_header!r}", flush=True)
        print(f"{student['name']}: {len(rows)} rows, {len(up)} upcoming, {len(over)} overdue, {len(low)} low,"
              f" now={current['class'] if current else None}", flush=True)


def main():
    mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mq.username_pw_set(E["MQTT_USERNAME"], E["MQTT_PASSWORD"])
    mq.will_set(f"{PREFIX}/status", "offline", retain=True)
    mq.connect(E.get("MQTT_HOST", "mosquitto"), int(E.get("MQTT_PORT", "1883")))
    mq.loop_start()
    mq.publish(f"{PREFIX}/status", "online", retain=True)
    while True:
        delay = None
        try:
            run_once(mq)
        except (SchoolToolError, Exception) as e:   # keep the loop alive, retry soon
            print(f"scrape failed: {type(e).__name__}: {e}", flush=True)
            delay = RETRY
        delay = min(delay, next_sleep()) if delay else next_sleep()
        print(f"next scrape in {int(delay)}s", flush=True)
        time.sleep(delay)


if __name__ == "__main__":
    main()
