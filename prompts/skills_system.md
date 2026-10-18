---
name: skills_system
version: 1
description: Short wrapper for deepagents SkillsMiddleware (progressive disclosure). The default is ~1K tokens.
inputs: []
---
## Skills
{skills_locations}{skills_load_warnings}
{skills_list}
To use a skill, `read_file` its SKILL.md (limit=1000) and follow it. The skill's tools become available after you
read it. Read the skill once per conversation, before its first use.
