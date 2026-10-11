"""SchoolTool parent-portal client: OIDC login, student switch, assignments table.

SchoolTool is ASP.NET WebForms, so there is no assignments API. We log in via the
IdentityServer OIDC form_post flow, then replay WebForms postbacks and parse the
DataGridAssignments table.
"""
import datetime as dt
import html
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (X11; Linux x86_64) schooltool-assignments/1.0"

# Hidden-input state the browser adds client-side; not present in the served HTML.
# Captured from a real session; the DX lists are constant per SchoolTool build.
EXTRA_FIELDS = {
    "6": "current",
    "Template1$Control0$PersonSummaryView$MobileMessageControl$MessageDialogState":
        "",
    "Template1$Control0$PersonSummaryView$SHPanel$personImage$EnlargePhotoState": "",
    "Template1$Control0$PersonSummaryView$StudentNameRecordAudioPopUpState": "",
}
TAB_ASSIGNMENTS = "Template1$Control0$TabHeader2$Menutabs1$MenuTabAssignments"
HOME_LINK = "Template1$MenuList1$DataList_Tabs$ctl01$SchoolToolLink_Tab"
TAB_SCHEDULE = "Template1$Control0$TabHeader2$Menutabs1$MenuTabSchedule"
GRID_TARGET = "Template1$Control0$MultiView$StudentScheduleView$ScheduleViewRB$1"


class SchoolToolError(Exception):
    pass


def _form_fields(form):
    """All submittable fields of a form, as a browser would send them."""
    out = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            out[name] = opt.get("value", "") if opt else ""
        elif el.name == "textarea":
            out[name] = el.text
        elif el.get("type") in ("checkbox", "radio"):
            if el.has_attr("checked"):
                out[name] = el.get("value", "on")
        elif el.get("type") in ("submit", "button", "image", "file"):
            continue
        else:
            out[name] = el.get("value", "")
    return out


class SchoolTool:
    def __init__(self, base_url, username, password):
        self.base = base_url.rstrip("/")  # e.g. https://host.schooltool.com/DISTRICT
        self.username, self.password = username, password
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.page = None

    # ---- login -----------------------------------------------------------
    def login(self):
        r = self.s.get(self.base, timeout=30)
        soup = BeautifulSoup(r.text, "lxml")
        form = next((f for f in soup.find_all("form")
                     if f.find("input", {"name": "password"})), None)
        if form is None:
            raise SchoolToolError(f"no login form at {r.url}")
        data = _form_fields(form)
        data["username"], data["password"] = self.username, self.password
        action = urljoin(r.url, form.get("action") or r.url)
        # The page's JS posts to Login?button=login&returnUrl=<authorize callback>;
        # the form action alone has no returnUrl and lands on the auth root.
        qs = parse_qs(urlparse(r.url).query)
        ret = (qs.get("ReturnUrl") or qs.get("returnUrl") or [""])[0]
        r = self.s.post(action, params={"button": "login", "returnUrl": ret},
                        data=data, timeout=30)
        # IdentityServer answers with an auto-submitting form_post to the app
        for _ in range(4):
            soup = BeautifulSoup(r.text, "lxml")
            form = soup.find("form")
            if form is None or not form.find("input", {"name": "id_token"}):
                break
            r = self.s.post(urljoin(r.url, form.get("action")),
                            data=_form_fields(form), timeout=30)
        if "Default.aspx" not in r.url or "MyHome" not in r.text:
            raise SchoolToolError(f"login failed (landed on {r.url})")
        self.page = r.text

    # ---- webforms helpers ---------------------------------------------------
    def _postback(self, target, argument="", page=None, extra=None):
        soup = BeautifulSoup(page or self.page, "lxml")
        form = soup.find("form")
        data = _form_fields(form)
        for k, v in EXTRA_FIELDS.items():
            data.setdefault(k, v)
        data.update(extra or {})
        data["__EVENTTARGET"], data["__EVENTARGUMENT"] = target, argument
        # the site posts multipart like a browser; match it
        r = self.s.post(f"{self.base}/Default.aspx",
                        files={k: (None, v) for k, v in data.items()}, timeout=60)
        r.raise_for_status()
        return r.text

    # ---- data -----------------------------------------------------------
    def students(self):
        # the webapi layer wants the page's SessionKey echoed in a header
        key = BeautifulSoup(self.page, "lxml").find("input", {"name": "SessionKey"})
        if key is None:
            raise SchoolToolError("SessionKey not found on page")
        r = self.s.get(f"{self.base}/webapi.mvc/schooltool/MyHome/GetContacts",
                       headers={"schooltool-header": key["value"],
                                "Accept": "application/json, text/plain, */*",
                                "Referer": f"{self.base}/Default.aspx"},
                       timeout=30)
        r.raise_for_status()
        return [
            {"id": c["Person_ID"], "guid": c["GUID_ID"], "name": c["FirstName"]}
            for c in r.json()["contacts"]
            if c.get("ShowSchedule") and c.get("StudentSchedule")
        ]

    def assignments(self, student):
        """Rows for one student, newest first, as SchoolTool lists them."""
        # The browser switches students from the home page; posting "select" from
        # another student's Assignments page silently keeps the old student.
        # (A bare GET of Default.aspx has no session state and re-runs the login.)
        back = self._postback(HOME_LINK, page=self.page)
        home = self._postback("select", student["guid"], page=back)
        page = self._postback(TAB_ASSIGNMENTS, page=home)
        pid = BeautifulSoup(page, "lxml").find(
            "input", {"name": re.compile(r"PersonSummaryView\$hfPersonID$")})
        if pid is None or pid.get("value") != str(student["id"]):
            raise SchoolToolError(
                f"student switch failed for {student['name']} "
                f"(page shows person {pid.get('value') if pid else None})")
        self.page = page
        self.last_header = header_text(page)
        return parse_assignments(page), parse_current_class(page)


    def bell_schedule(self, student_page=None):
        """Period times from the Schedule tab's Grid View (start and end per period).

        Call right after assignments(); navigates from that page without changing
        self.page. Bell times don't change day to day, so callers cache this.
        """
        page = student_page or self.page
        sched = self._postback(TAB_SCHEDULE, page=page)
        radio = BeautifulSoup(sched, "lxml").find("input", {"type": "radio", "value": "Grid"})
        if radio is None:
            raise SchoolToolError("schedule Grid View radio not found")
        grid = self._postback(GRID_TARGET, page=sched, extra={radio["name"]: "Grid"})
        bell = parse_bell(grid)
        if not bell:
            raise SchoolToolError("no period times found in schedule grid")
        return bell


# Grid View rows read "Period 6 Time 12:21 - 1:09" with no AM/PM; school runs ~7:45-3.
_BELL = re.compile(r"Period\s+(\w+)\s+Time\s+(\d{1,2}):(\d\d)\s*-\s*(\d{1,2}):(\d\d)")


def _minutes(h, m):
    h = int(h)
    if h < 7:          # 1:10 .. 6:xx are afternoon; 12:xx is already noon
        h += 12
    return h * 60 + int(m)


def _fmt(mins):
    h, m = divmod(mins, 60)
    return f"{(h - 1) % 12 + 1}:{m:02d}{'AM' if h < 12 else 'PM'}"


def parse_bell(page_html):
    text = re.sub(r"\s+", " ", BeautifulSoup(page_html, "lxml").get_text(" "))
    slots = {}
    for label, h1, m1, h2, m2 in _BELL.findall(text):
        if label.upper() == "DAILY":      # attendance, not a class period
            continue
        slots[label] = (_minutes(h1, m1), _minutes(h2, m2))
    return [{"period": f"Period {k}", "start": s, "end": e,
             "times": f"{_fmt(s)} to {_fmt(e)}"}
            for k, (s, e) in sorted(slots.items(), key=lambda kv: kv[1])]


def period_at(bell, minutes):
    """The bell slot containing this minute-of-day, or None (passing time, off hours)."""
    return next((b for b in bell if b["start"] <= minutes <= b["end"]), None)


# The student header reads e.g. "Cycle day 4, Period 6 (11:18AM to 11:58AM)<br />
# LIFE SCI 6 in Room 442 Teacher: ..." — the school picks the section, so there is
# no schedule/cycle-day/marking-period math on our side. It sits HTML-escaped inside
# a JS string, hence the unescape. "Periods 6 to 7A" happens for double periods.
_CURRENT = re.compile(
    r"Cycle day [^,<]+,\s*(Periods? [^(<]+?)\s*\(([^)]+)\)\s*<br\s*/?>\s*"
    r"([^<]+?)(?:\s+in Room [^<]*)?</font>")


def parse_current_class(page_html):
    m = _CURRENT.search(html.unescape(page_html))
    if not m:
        return None
    period, times, name = (re.sub(r"\s+", " ", g).strip() for g in m.groups())
    return {"period": period, "times": times, "class": name}


def header_text(page_html):
    """Raw student-header text, for logging when no class matches (off hours)."""
    m = re.search(r'InlineSection"?>\s*<b>[^<]*</b></h2></nobr></td><td[^>]*>(.*?)</td>',
                  html.unescape(page_html), re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""


def parse_assignments(page_html):
    soup = BeautifulSoup(page_html, "lxml")
    grid = soup.find(id=re.compile(r"DataGridAssignments$"))
    if grid is None:
        raise SchoolToolError("assignments table not found")
    rows = []
    for tr in grid.find_all("tr", class_=re.compile(r"DataGrid(Alternate)?ItemStyle")):
        c = [re.sub(r"\s+", " ", html.unescape(td.get_text(" "))).strip()
             for td in tr.find_all("td", recursive=False)]
        if len(c) < 6:
            continue
        course, category, name, due, mx, score = c[:6]
        rows.append({
            "course": course, "category": category, "name": name,
            "due": dt.datetime.strptime(due, "%m/%d/%Y").date(),
            "max": float(mx) if mx else 0.0, "score": score,
        })
    return rows


def classify(rows, today):
    """Split into upcoming / overdue-or-ungraded. SchoolTool has no missing flag."""
    upcoming, overdue = [], []
    for r in rows:
        if r["score"] != "":          # graded, or '[ S ]' special/excused
            continue
        if r["max"] <= 0:
            continue
        (upcoming if r["due"] >= today else overdue).append(r)
    upcoming.sort(key=lambda r: r["due"])
    overdue.sort(key=lambda r: r["due"], reverse=True)
    return upcoming, overdue


def low_grades(rows, threshold):
    """Graded rows scoring at or below `threshold` (a fraction), lowest first.

    Excused/incomplete marks like '[ S ]' or '[ IN ]' aren't numeric and are skipped,
    as are zero-point assignments.
    """
    low = []
    for r in rows:
        try:
            score = float(r["score"])
        except ValueError:
            continue
        if r["max"] > 0 and score / r["max"] <= threshold:
            low.append({**r, "score": score, "pct": round(100 * score / r["max"], 1)})
    low.sort(key=lambda r: (r["pct"], r["due"]))
    return low
