"""Drive the demo UI in a headless browser like a judge, check every step, save a screenshot of each.

    PLAYWRIGHT_BROWSERS_PATH=var/ms-playwright .venv/bin/python scripts/demo_ui_check.py
    PLAYWRIGHT_BROWSERS_PATH=var/ms-playwright .venv/bin/python scripts/demo_ui_check.py --only 02_siddharth_discount
    ... scripts/demo_ui_check.py --reset   # first undo what earlier runs saved (``reset`` in the config)
    ... scripts/demo_ui_check.py --reset --record   # also film it: one clip per step in var/demo-run/video/

Needs the running UI (``npx vite`` in frontend/) and API (``nwb serve``), see docs/DEMO.md. Steps and the
expected answers live in ``demo/ui_check.yaml``. Exits non-zero when any step fails.
One-time setup, local to the repo: ``uv pip install --python .venv/bin/python playwright`` and
``PLAYWRIGHT_BROWSERS_PATH=var/ms-playwright .venv/bin/python -m playwright install chromium-headless-shell``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import Page, ViewportSize, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from nwb.obs.log import get_logger, setup_logging

log = get_logger(__name__)

PERSONA = 'select[aria-label="Acting as"]'
LAST_BRAIN = ".turn.brain >> nth=-1"
EVIDENCE = '[data-testid="evidence"]'  # the right column's Evidence tab, showing the latest answer
DONE = re.compile(r"answered in|error", re.I)
CLEAN_END = re.compile(r"[.!?)\]】:`'\"]\s*$")


@dataclass
class Outcome:
    id: str
    ok: bool
    seconds: float
    screenshot: str
    answer: str = ""
    problems: list[str] = field(default_factory=list)


def _flags(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I | re.M) for p in patterns]


def _choose(page: Page, persona: str) -> None:
    if page.locator(PERSONA).input_value() != persona:
        page.select_option(PERSONA, persona)
        page.wait_for_selector(".composer input")


def _chat(page: Page, step: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str, list[str]]:
    _choose(page, step["persona"])
    before = page.locator(".turn.brain").count()
    delay = cfg.get("_type_delay_ms", 0)
    if delay:  # recording: type visibly, as a person would
        page.locator(".composer input").press_sequentially(step["message"], delay=delay)
    else:
        page.fill(".composer input", step["message"])
    page.click(".composer button[type=submit]")
    asked = time.monotonic()
    page.wait_for_function("n => document.querySelectorAll('.turn.brain').length > n", arg=before, timeout=10_000)
    status = page.locator(f"{LAST_BRAIN} >> .status")
    status.wait_for(timeout=cfg["answer_timeout_s"] * 1000)
    cfg.setdefault("_waits", []).append((asked, time.monotonic()))  # the model wait, for cutting the video
    # Models emit no-break and narrow spaces ("Alok\u202fNaik"); compare on plain spaces.
    answer = re.sub(r"[\u00a0\u2007\u2009\u202f]", " ", page.locator(f"{LAST_BRAIN} >> .answer").inner_text()).strip()
    # Prose without citation chips: a reply that ends in a citation still has to end its sentence.
    # A reply whose last block is a list (a bullet preference, a to-do list) needs no closing full stop.
    prose = page.locator(f"{LAST_BRAIN} >> .answer").evaluate(
        "el => { const c = el.cloneNode(true); c.querySelectorAll('code.cite').forEach(n => n.remove());"
        " [...c.children].filter(n => !n.innerText.trim()).forEach(n => n.remove());"
        " const last = c.lastElementChild; return last && /^(UL|OL)$/.test(last.tagName) ? '' : c.innerText.trim(); }"
    )
    page.click('[data-testid="tab-evidence"]')
    panel = page.locator(EVIDENCE)
    sources = panel.inner_text() if panel.count() else ""
    problems: list[str] = []
    if not DONE.search(status.inner_text()) or "error" in status.inner_text().lower():
        problems.append(f"status: {status.inner_text()!r}")
    if not answer:
        problems.append("empty answer")
    elif prose and not CLEAN_END.search(prose):
        problems.append("answer looks cut off (no closing punctuation)")
    for rx in _flags(step.get("expect", [])):
        if not rx.search(answer):
            problems.append(f"missing /{rx.pattern}/")
    for rx in _flags(cfg.get("forbid_always", []) + step.get("forbid", [])):
        if rx.search(answer):
            problems.append(f"forbidden /{rx.pattern}/")
    for rx in _flags(step.get("sources", [])):
        if not rx.search(sources):
            problems.append(f"Evidence panel lacks /{rx.pattern}/")
    return answer, sources, problems


def _card(page: Page, title: str) -> Any:
    return page.locator(".inbox .approval-item", has=page.locator(".title", has_text=title)).first


def run_step(page: Page, step: dict[str, Any], cfg: dict[str, Any], out: Path) -> Outcome:
    started = time.monotonic()
    answer, problems = "", []
    kind = step["kind"]
    if kind == "personas":
        page.locator(PERSONA).wait_for()
        ids = page.locator(f"{PERSONA} option").all_inner_texts()
        problems = [f"persona {pid} missing" for pid in step["expect_ids"] if not any(f"({pid})" in t for t in ids)]
        answer = "; ".join(ids)
    elif kind == "chat":
        answer, sources, problems = _chat(page, step, cfg)
        if sources:
            answer = f"{answer}\n[Evidence] {sources}"
    elif kind == "end_session":
        page.click("text=End session")
        page.locator(".turn.brain", has_text="Session ended").last.wait_for(timeout=60_000)
        answer = page.locator(LAST_BRAIN).inner_text()
    elif kind in {"approvals", "approve"}:
        _choose(page, step["persona"])
        page.click('[data-testid="tab-approvals"]')
        card = _card(page, step["expect_title"])
        try:
            card.wait_for(timeout=20_000)
            answer = card.inner_text()
            if kind == "approve":
                card.locator("button", has_text="Approve").click()
                card.wait_for(state="detached", timeout=30_000)
                answer = page.locator(".inbox .notice").inner_text(timeout=10_000)
                if not answer.startswith("Approved"):
                    problems.append(f"no approval confirmation: {answer!r}")
        except PlaywrightTimeout as exc:  # a missing card is a failed step, reported below
            problems.append(f"approval card {step['expect_title']!r}: {type(exc).__name__}")
    else:
        problems.append(f"unknown step kind {kind}")
    shot = out / f"{step['id']}.png"
    page.screenshot(path=str(shot), full_page=True)
    return Outcome(step["id"], not problems, time.monotonic() - started, str(shot), answer, problems)


def reset_demo(rules: list[dict[str, str]]) -> None:
    """Remove the lines earlier runs saved (e.g. Pooja's bullet preference) so the demo shows a fresh save.
    Only lines matching a rule's ``drop`` pattern go; the rest of the memory file is kept. Uses the API's env."""
    from nwb.config import get_settings
    from nwb.container import Container

    c = Container.from_settings(get_settings())
    try:
        for rule in rules:
            text = c.memory_files.read(c.settings.company_id, rule["person"], rule["file"]) or ""
            drop = re.compile(rule["drop"], re.I)
            kept = [line for line in text.splitlines() if not drop.search(line)]
            removed = len(text.splitlines()) - len(kept)
            if removed:
                c.memory_files.write(c.settings.company_id, rule["person"], rule["file"], "\n".join(kept) + "\n")
            log.info("demo reset", person=rule["person"], file=rule["file"], removed=removed)
            print(f"reset {rule['person']} {rule['file']}: removed {removed} line(s)")
    finally:
        c.close()


@dataclass
class Segment:
    id: str
    start: float
    end: float
    waits: list[tuple[float, float]] = field(default_factory=list)  # model waits, seconds into the recording


def group_clips(segments: list[Segment], clips: dict[str, list[str]]) -> list[Segment]:
    """Join step segments into the named clips (config ``record.clips``); waits become clip-relative."""
    by_id = {s.id: s for s in segments}
    out: list[Segment] = []
    for name, ids in clips.items():
        parts = [by_id[i] for i in ids if i in by_id]
        if not parts:
            continue
        start, end = min(p.start for p in parts), max(p.end for p in parts)
        waits = [(round(a - start, 1), round(b - start, 1)) for p in parts for a, b in p.waits]
        out.append(Segment(name, start, end, waits))
    return out


def zoom_on_sources(page: Page, rec: dict[str, Any], t0: float) -> Segment:
    """A close-up of the Evidence panel (reasoning graph and FalkorDB sources) for the last answer, for the video."""
    start = time.monotonic() - t0
    page.click('[data-testid="tab-evidence"]')
    panel = page.locator(EVIDENCE)
    page.evaluate(f"document.body.style.zoom = '{rec['zoom']}'")
    panel.scroll_into_view_if_needed()
    page.evaluate("el => el.scrollIntoView({block: 'center'})", panel.element_handle())
    page.wait_for_timeout(int(rec["zoom_hold_s"] * 1000))
    page.evaluate("document.body.style.zoom = '1'")
    return Segment(rec["zoom_clip"], start, time.monotonic() - t0)


def cut_clips(full: Path, segments: list[Segment], out: Path) -> None:
    """One webm per segment (and an mp4 copy) from the single recording, with the ffmpeg in the repo .venv."""
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]
    except ImportError:
        print("imageio-ffmpeg is not in the .venv: kept only the full recording", full)
        return
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    manifest: list[dict[str, Any]] = []
    for seg in segments:
        span = ["-ss", f"{seg.start:.2f}", "-to", f"{seg.end:.2f}", "-i", str(full)]
        webm, mp4 = out / f"{seg.id}.webm", out / f"{seg.id}.mp4"
        vp8 = ["-c:v", "libvpx", "-b:v", "4M", "-deadline", "realtime", "-cpu-used", "8", "-an"]
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", *span, *vp8, str(webm)], check=True)  # noqa: S603 - our ffmpeg, our args
        h264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-an"]
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", *span, *h264, str(mp4)], check=True)  # noqa: S603 - our ffmpeg, our args
        _frames, secs = imageio_ffmpeg.count_frames_and_secs(str(mp4))
        manifest.append(
            {"clip": seg.id, "mp4": str(mp4), "webm": str(webm), "seconds": round(secs, 1), "waits": seg.waits}
        )
        print(f"clip {seg.id}: {secs:.1f} s  {mp4}  model waits (s): {seg.waits}")
    (out / "clips.json").write_text(json.dumps(manifest, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--config", type=Path, default=Path("demo/ui_check.yaml"))
    parser.add_argument("--out", type=Path, default=Path("var/demo-run/ui"))
    parser.add_argument("--only", nargs="*", help="step ids to run (default: all)")
    parser.add_argument("--reset", action="store_true", help="apply the config's reset rules first")
    parser.add_argument("--record", action="store_true", help="film the run: one clip per step (config `record`)")
    args = parser.parse_args()
    setup_logging()
    cfg = yaml.safe_load(args.config.read_text())
    if args.reset:
        reset_demo(cfg.get("reset", []))
    steps = [s for s in cfg["steps"] if not args.only or s["id"] in args.only]
    args.out.mkdir(parents=True, exist_ok=True)
    results: list[Outcome] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        rec: dict[str, Any] = cfg["record"] if args.record else {}
        segments: list[Segment] = []
        if rec:
            video_dir = Path(rec["dir"])
            (video_dir / "raw").mkdir(parents=True, exist_ok=True)
            size: ViewportSize = {"width": rec["width"], "height": rec["height"]}
            context = browser.new_context(
                viewport=size, record_video_dir=str(video_dir / "raw"), record_video_size=size
            )
            cfg["_type_delay_ms"] = rec["type_delay_ms"]
        else:
            context = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        t0 = time.monotonic()
        page.goto(args.base_url)
        for step in steps:
            start = time.monotonic() - t0
            cfg["_waits"] = []
            outcome = run_step(page, step, cfg, args.out)
            if rec:
                page.wait_for_timeout(int(rec["hold_s"] * 1000))  # let the viewer read the result
                waits = [(a - t0, b - t0) for a, b in cfg["_waits"]]
                segments.append(Segment(step["id"], start, time.monotonic() - t0, waits))
                if step["id"] == rec.get("zoom_after"):
                    segments.append(zoom_on_sources(page, rec, t0))
            results.append(outcome)
            log.info("ui step %s %s %.1fs", outcome.id, "PASS" if outcome.ok else "FAIL", outcome.seconds)
            print(f"{'PASS' if outcome.ok else 'FAIL'} {outcome.id} ({outcome.seconds:.1f} s) {outcome.screenshot}")
            for problem in outcome.problems:
                print(f"     ! {problem}")
            print("     " + outcome.answer.replace("\n", "\n     ")[:1200])
        video = page.video.path() if rec and page.video else None
        context.close()  # finishes the recording
        browser.close()
    if video:
        full = Path(rec["dir"]) / "full_run.webm"
        Path(video).replace(full)
        cut_clips(full, group_clips(segments, rec["clips"]), Path(rec["dir"]))
    failed = [r.id for r in results if not r.ok]
    print(f"{len(results) - len(failed)}/{len(results)} steps passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
