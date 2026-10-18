# USER.md - about {{ name }}

Who you are helping. The directory block is refreshed when HR data changes. Everything outside it is yours and the
user's: preferences and directives stay put. Edit it with edit_file. Keep it under ~40 lines.

<!-- nwb:directory -->
## Profile (from the directory, {{ asof }})
- Name: {{ name }} ({{ person_id }})
- Role: {{ title or "-" }}{% if grade %}, grade {{ grade }}{% endif %}
- Team: {{ units or "-" }}
- Manager: {{ manager or "-" }}
- Location: {{ location or "-" }}{% if hired_on %}
- Joined: {{ hired_on }}{% endif %}
<!-- /nwb:directory -->

## How they like answers
<!-- Directives: one pair each. A line `observed: YYYY-MM-DD | status: active` or superseded, then Always, Never or Prefer. -->
- (nothing yet: ask or observe, then write it here)

## Working context
- (current goals, projects and people they work with, as they tell you)

## Directives
- (none yet)
