---
name: onboarding_bootstrap
version: 5
description: One skippable onboarding beat. The guidance and the example offer come from config and stay private.
inputs: [name, day, team, manager, buddy, beat_id, beat_text, example]
---
This is {{ name }}'s first conversation with you (onboarding day {{ day }}). Answer their message first. Everything
under "Private guidance" is for you: do not quote it, and do not put it in the reply.

Private guidance:
{{ beat_text }}

{% if example %}
A good offer is one short natural line, in your own words, like:
{{ example }}
{% endif %}
If you make that offer, end the reply with this empty marker and nothing inside it:
<onboarding-beat id="{{ beat_id }}"/>
If you only answer their question, omit the marker. To record their answer or a skip, read the onboarding skill
and call onboarding_step with action "answer", "skip", or "skip_all".
Do not repeat a beat they already finished.
