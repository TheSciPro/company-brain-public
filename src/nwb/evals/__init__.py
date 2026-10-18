"""Agent-level evals: natural-language questions through the whole agent, asserted on tool artifacts.

``run_live`` talks to the configured LLM chain and records a cassette (every model response, in call order) plus
a markdown transcript. ``run_replay`` feeds a cassette back through a scripted model: tools, skills, memory and the
database run for real, so a stale cassette fails as soon as the tools stop returning the expected value.
"""

from nwb.evals.cases import EvalCase, Expect, PersonaSpec, load_cases
from nwb.evals.runner import EvalResult, EvalRunner

__all__ = ["EvalCase", "EvalResult", "EvalRunner", "Expect", "PersonaSpec", "load_cases"]
