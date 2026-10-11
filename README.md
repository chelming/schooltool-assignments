# schooltool-assignments

Scrapes the SchoolTool parent portal on an interval and publishes per-student
sensors to Home Assistant over MQTT (HA discovery). For each student you get:

- **Current class**: the period in progress (from the bell schedule), or "No class"
- **Upcoming assignments** and **Overdue or ungraded assignments**: a count as the
  state, with the list in the attributes (capped at 30 items)
- **Graded 75% or lower**: graded assignments at or below `LOW_GRADE_PCT`, lowest first,
  with `score` and `percent` on each item

SchoolTool is ASP.NET WebForms with no assignments API, so `schooltool.py` logs in
through the IdentityServer OIDC `form_post` flow, then replays WebForms postbacks
and parses the assignments table. Expect it to need maintenance if the portal changes.

## Run

A GitHub Action builds and pushes `ghcr.io/chelming/schooltool-assignments` on every
push to `main` (tags: `latest` and `sha-<commit>`). To run it:

```bash
cp .env.example .env     # fill in the portal and MQTT credentials
docker compose up -d
```

If the package is private, `docker login ghcr.io` first (or make the package public).
The container needs to reach your MQTT broker; put it on the broker's Docker network
if needed. Sensors are published under the `schooltool/` topic prefix with HA
discovery on `homeassistant/`, so they appear in HA automatically as one device per
student ("SchoolTool <first name>").

### docker-compose.yml

```yaml
services:
  schooltool:
    image: ghcr.io/chelming/schooltool-assignments:${TAG:-latest}
    container_name: schooltool
    restart: unless-stopped
    env_file:
      - .env
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SCHOOLTOOL_URL` | required | Portal base URL including the district path |
| `SCHOOLTOOL_USERNAME` | required | Parent portal login |
| `SCHOOLTOOL_PASSWORD` | required | Parent portal password |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | required | Broker credentials |
| `MQTT_HOST` / `MQTT_PORT` | `mosquitto` / `1883` | Broker address |
| `INTERVAL_MINUTES` | `60` | Scrape interval (it also re-scrapes at each bell and nightly at 01:00) |
| `LOW_GRADE_PCT` | `75` | Graded assignments at or below this percent show in the low-grades sensor |
| `BELL_TIMES` | unset | Comma-separated 24h period start times, e.g. `07:45,08:35,...`. Each one triggers a re-scrape; include an end-of-day time so the current-class sensor flips to "No class". Unset = no bell-time scrapes, only the interval poll and the nightly run |
| `TZ` | `America/New_York` | Timezone for the bell schedule |
| `TOPIC_PREFIX` | `schooltool` | MQTT topic prefix for state/attributes |
| `DISCOVERY_PREFIX` | `homeassistant` | HA MQTT discovery prefix |
| `TAG` | `latest` | Image tag (compose only) |

Example `.env`:

```ini
# Image tag to run (latest, or a sha-<commit> tag)
TAG=latest

# Parent portal (required). URL includes the district path, e.g. https://host.schooltool.com/DISTRICT
SCHOOLTOOL_URL=
SCHOOLTOOL_USERNAME=
SCHOOLTOOL_PASSWORD=

# MQTT broker (required credentials)
MQTT_HOST=mosquitto
MQTT_PORT=1883
MQTT_USERNAME=
MQTT_PASSWORD=

# Optional
TZ=America/New_York
INTERVAL_MINUTES=60
# Comma-separated 24h period start times (add an end-of-day time); unset = interval polling only
#BELL_TIMES=07:45,08:35,09:24,15:00
TOPIC_PREFIX=schooltool
DISCOVERY_PREFIX=homeassistant
```

Keep `.env` out of git (it is in `.gitignore`).

## Home Assistant

Each student gets three sensors. The entity IDs below use a student called Alex; check
yours under Developer Tools > States (filter on `schooltool`) and adjust.

| Sensor | State | Attributes |
|---|---|---|
| `sensor.schooltool_alex_current_class` | `3 (09:24-10:13) Biology`, or `No class` | `period`, `times`, `class`, `updated` |
| `sensor.schooltool_alex_upcoming_assignments` | count | `lines`, `items`, `next_due`, `updated` |
| `sensor.schooltool_alex_overdue_assignments` | count | `lines`, `items`, `updated` |
| `sensor.schooltool_alex_graded_75_or_lower` | count | `lines`, `items`, `updated` |

Each entry in `items` has `course`, `name`, `due` (ISO date), `category` and `points`.
`lines` is the same list pre-formatted as `COURSE - Teacher - Assignment - M/D/YYYY`.
The sensors go unavailable if the container stops (MQTT last-will).

### Dashboard: assignments card

```yaml
type: markdown
title: Alex
content: >
  **Now:** {{ states('sensor.schooltool_alex_current_class') }}

  **Overdue ({{ states('sensor.schooltool_alex_overdue_assignments') }})**
  {% for l in state_attr('sensor.schooltool_alex_overdue_assignments', 'lines') or [] %}
  - {{ l }}
  {%- endfor %}

  **Upcoming ({{ states('sensor.schooltool_alex_upcoming_assignments') }})**
  {% for l in (state_attr('sensor.schooltool_alex_upcoming_assignments', 'lines') or [])[:8] %}
  - {{ l }}
  {%- endfor %}
```

### Dashboard: at-a-glance

```yaml
type: entities
title: School
entities:
  - entity: sensor.schooltool_alex_current_class
  - entity: sensor.schooltool_alex_upcoming_assignments
  - entity: sensor.schooltool_alex_overdue_assignments
```

### Automation: weekday overdue reminder

```yaml
alias: School - overdue assignments reminder
trigger:
  - platform: time
    at: "16:00:00"
condition:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
  - condition: numeric_state
    entity_id: sensor.schooltool_alex_overdue_assignments
    above: 0
action:
  - service: notify.notify
    data:
      title: "Alex has overdue assignments"
      message: >
        {{ state_attr('sensor.schooltool_alex_overdue_assignments', 'lines')[:5] | join('\n') }}
```

### Template sensor: assignments due tomorrow

```yaml
template:
  - sensor:
      - name: "Alex due tomorrow"
        unit_of_measurement: assignments
        state: >
          {% set tomorrow = (now() + timedelta(days=1)).date().isoformat() %}
          {{ (state_attr('sensor.schooltool_alex_upcoming_assignments', 'items') or [])
             | selectattr('due', 'eq', tomorrow) | list | count }}
```

### Automation: announce the next class

```yaml
alias: School - class changed
trigger:
  - platform: state
    entity_id: sensor.schooltool_alex_current_class
condition:
  - condition: template
    value_template: "{{ trigger.to_state.state not in ['No class', 'unavailable', 'unknown'] }}"
action:
  - service: input_text.set_value
    target: {entity_id: input_text.alex_class}
    data: {value: "{{ trigger.to_state.attributes.class or 'Free period' }}"}
```

## License

[GNU AGPL v3](LICENSE). Free to use, modify and share. If you distribute a modified version,
or run one as a network service, you must make your source available under the same license.
