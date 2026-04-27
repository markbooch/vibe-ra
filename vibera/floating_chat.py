"""
Always-on-top floating chat window for the OpenRA voice commander.

PyObjC implementation. An earlier prototype used Tk, but system Python's
Tk 8.5 on recent macOS releases no longer renders self-drawn widgets
(Labels are invisible, only native Cocoa widgets like Button paint). So
we drive Cocoa directly via pyobjc, which gives us proper rendering,
real NSFloatingWindowLevel for always-on-top (no Tk -topmost contentView
blanking bug), and free IME support in NSTextView.

Run:
    GEMINI_API_KEY=... python -m vibera

Layout (top -> bottom):
    [titlebar (native, draggable, close button)]
    [status row     ]   "linked <host>:<port>" / "thinking…"
    [chat scroll    ]   bubbles, user right (blue), bot left (grey)
    [input NSTextView]  Enter = send, Shift+Enter = newline
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Optional

# Surface daemon / adviser INFO logs (they're silent by default since
# nobody installs a root handler). Goes to stderr → /tmp/floating_chat.log
# when the process is started under nohup.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import objc
from AppKit import (
    NSApplication, NSApp, NSWindow, NSView, NSScrollView, NSTextView,
    NSTextField, NSColor, NSFont, NSMakeRect, NSMakeSize, NSMakePoint,
    NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskMiniaturizable,
    NSViewWidthSizable, NSViewHeightSizable, NSViewMinYMargin,
    NSViewMaxYMargin,
    NSAttributedString, NSMutableParagraphStyle, NSParagraphStyleAttributeName,
    NSForegroundColorAttributeName, NSFontAttributeName,
    NSLineBreakByWordWrapping,
    NSEventModifierFlagShift,
    NSButton, NSButtonTypeSwitch, NSButtonTypeMomentaryPushIn,
    NSBezelStyleRounded, NSBezelStyleInline, NSControlStateValueOn,
    NSControlStateValueOff, NSLineBreakByTruncatingTail,
)
from Foundation import NSObject, NSMakeRange

from .openra_client import OpenRAClient
from .task_translator import build_task, translate_to_plan
from .voice_commander import snapshot_to_lean_state
from .daemon import TaskDaemon
from .task import Task
from .adviser import AdviserLoop
from .build_order import BuildOrderRunner
from .commander import Commander, PlanStore
from .events import EventBus
from .snapshot_pump import SnapshotPump
from .reactors import AutoPlacer, Recovery, Repairer, StanceNudger
from .army_reactors import (
    ArmyCommander, ArmyProducer, DefenseLayer, EconomyScaler, Scout,
    TechBuilder,
)


# ---------- locate OpenRA window (reused from earlier) ----------

def find_openra_window() -> Optional[tuple[int, int, int, int]]:
    try:
        import Quartz  # type: ignore
    except ImportError:
        return None
    opts = (Quartz.kCGWindowListOptionAll
            | Quartz.kCGWindowListExcludeDesktopElements)
    wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
    best, best_area = None, 0
    for w in wins:
        owner = (w.get("kCGWindowOwnerName") or "").lower()
        if "dotnet" not in owner and "openra" not in owner:
            continue
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        b = w.get("kCGWindowBounds") or {}
        ww, hh = int(b.get("Width", 0)), int(b.get("Height", 0))
        if ww < 600 or hh < 400:
            continue
        area = ww * hh
        if area > best_area:
            best_area = area
            best = (int(b.get("X", 0)), int(b.get("Y", 0)), ww, hh)
    return best


# ---------- visual constants ----------

def rgb(r, g, b, a=1.0):
    return NSColor.colorWithRed_green_blue_alpha_(r/255, g/255, b/255, a)

BG_WIN     = rgb(28, 31, 38)      # window background
BG_CHAT    = rgb(28, 31, 38)
USER_BG    = rgb(59, 130, 246)    # blue
USER_FG    = rgb(255, 255, 255)
BOT_BG     = rgb(42, 47, 58)
BOT_FG     = rgb(230, 232, 236)
META_FG    = rgb(107, 114, 128)
ERR_FG     = rgb(248, 113, 113)
ACCENT     = rgb(16, 185, 129)
INPUT_BG   = rgb(37, 41, 51)
INPUT_FG   = rgb(230, 232, 236)


# ---------- formatting LLM result ----------

def fmt_step(step: dict) -> str:
    """One-line summary of a step dict (after Step.to_dict())."""
    kind = step.get("kind", "?")
    if kind == "action":
        verb = step.get("verb", "?")
        params = step.get("params") or {}
        bits = [verb]
        if "item" in params:
            cnt = params.get("count", 1)
            bits.append(f"{params['item']}×{cnt}" if cnt != 1 else params["item"])
        if "actor_id" in params:
            bits.append(f"#{params['actor_id']}")
        if "target_id" in params:
            bits.append(f"->#{params['target_id']}")
        if "x" in params and "y" in params:
            bits.append(f"->({params['x']},{params['y']})")
        return " ".join(bits)
    if kind == "wait":
        u = step.get("until") or {}
        args = u.get("args") or {}
        body = u.get("kind", "?")
        if args:
            body += "(" + ",".join(f"{k}={v}" for k, v in args.items()) + ")"
        timeout = step.get("timeout_ticks")
        if timeout:
            body += f" ≤{timeout}t"
        return f"wait: {body}"
    if kind == "branch":
        u = step.get("until") or {}
        return f"branch: {u.get('kind', '?')}"
    return kind


def fmt_plan(plan: dict) -> tuple[str, bool]:
    """Render a fresh plan (right after the LLM returned it) as a bot bubble."""
    if plan.get("_error"):
        raw = (plan.get("_raw") or "")[:200]
        return f"LLM error: {plan['_error']}\n{raw}", True
    intent = plan.get("intent") or "(no intent)"
    conf = plan.get("confidence")
    lat = plan.get("_latency_sec")
    lines = [f"plan: {intent}  conf={conf}  ({lat}s)"]
    reasoning = plan.get("reasoning")
    if reasoning:
        lines.append(reasoning)
    steps = plan.get("steps") or []
    if steps:
        lines.append("")
        for i, s in enumerate(steps):
            lines.append(f"  {i}. {fmt_step(s)}")
    return "\n".join(lines), False


def fmt_task_change(t: Task) -> tuple[str, bool]:
    """Short status line for a task whose state moved since we last looked."""
    intent = t.intent or "(task)"
    state = t.state
    cursor = t.cursor
    total = len(t.steps)
    if state == "done":
        return f"✓ done [{t.id}] {intent}  ({total}/{total})", False
    if state == "partial":
        # Some action steps were rejected but the rest ran. Show how many
        # succeeded so the user can decide whether to follow up.
        bad = sum(1 for s in t.steps if s.failed)
        good = total - bad
        return (
            f"⚠ partial [{t.id}] {intent}  {good}/{total} ok, {bad} rejected\n"
            f"   {t.error or ''}"
        ), True
    if state == "failed":
        return f"✗ failed [{t.id}] {intent}  step {cursor}/{total}\n   {t.error or ''}", True
    if state == "cancelled":
        return f"⊘ cancelled [{t.id}] {intent}", True
    if state == "active":
        cur_step = t.steps[cursor] if cursor < total else None
        cur_desc = fmt_step(cur_step.to_dict()) if cur_step else "—"
        return f"… [{t.id}] {intent}  step {cursor + 1}/{total}: {cur_desc}", False
    return f"[{t.id}] {state} {cursor}/{total}", False


# ---------- input text view that handles Enter ----------

class CommandTextView(NSTextView):
    """NSTextView subclass: plain Enter submits, Shift+Enter inserts newline."""

    def initWithFrame_(self, frame):  # noqa: N802
        self = objc.super(CommandTextView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._on_submit = None
        return self

    def setSubmitHandler_(self, fn):  # noqa: N802
        self._on_submit = fn

    def keyDown_(self, event):  # noqa: N802
        # 0x24 = Return, 0x4C = numeric-pad Enter
        if event.keyCode() in (0x24, 0x4C):
            shift = bool(event.modifierFlags() & NSEventModifierFlagShift)
            if not shift and self._on_submit is not None:
                text = str(self.string()).strip()
                if text:
                    self.setString_("")
                    self._on_submit(text)
                return
        objc.super(CommandTextView, self).keyDown_(event)


# ---------- controller ----------

class ChatController(NSObject):
    """Owns the window. Bridges background worker results to the UI on the
    main thread via performSelectorOnMainThread_."""

    # ----- construction -----

    def initWithHost_port_(self, host, port):  # noqa: N802
        self = objc.super(ChatController, self).init()
        if self is None:
            return None
        self.host = host
        self.port = port
        self.work_q: queue.Queue[str] = queue.Queue()
        self.busy = False
        # Snapshot of {task_id: (state, cursor)} from the daemon's last
        # on_change callback. We only render a bubble when this changes
        # so the chat doesn't get spammed by stable ticks.
        self.task_view = {}            # type: dict[str, tuple[str, int]]
        self.task_view_lock = threading.Lock()
        # Latest suggestion list shown in the adviser bar. Indexed by
        # button tag (0..2) so the click handler can find the plan.
        self.adviser_suggestions: list[dict] = []
        # Master automation switch — when False, BO + R1-R5 + AutoPlacer
        # + StanceNudger + Repairer all stay silent. Recovery still runs
        # so the watchdog keeps the daemon healthy. Toggle from the UI
        # button labelled "Auto" in the status bar. Plain bool is fine —
        # all readers grab a snapshot via lambda each event.
        self._automation_enabled = True
        # --- Event-driven backbone (v2) ---
        # SnapshotPump owns the only OpenRA socket and ticks at 1Hz; every
        # other component subscribes to its events through the EventBus.
        # Reactors handle deterministic responses (placement, stance,
        # recovery); BuildOrderRunner drives the opening; AdviserLoop only
        # consults the LLM on real transitions.
        self.bus = EventBus()
        self.pump = SnapshotPump(self.bus, host=host, port=port, hz=1.0)
        # Daemon now subscribes to TickEvent for its execution loop and
        # uses the pump's shared (locked) client for command dispatch.
        self.daemon = TaskDaemon(
            bus=self.bus,
            client=self.pump.client,
            on_change=self.__onDaemonChange,
        )
        # BuildOrderRunner: deterministic RA1 opening (fact -> powr -> proc
        # -> barr/tent -> weap -> harv2 -> powr2). Submits tasks straight
        # into the daemon. Stays "active" until every goal is met OR
        # Recovery force-emits OpeningComplete after the BO timeout. The
        # adviser checks `is_active()` and suppresses its own econ verbs
        # while we're running.
        self.build_order = BuildOrderRunner(
            bus=self.bus,
            add_task=self.daemon.add_task,
            tasks_provider=self.daemon.snapshot_tasks,
            is_master_enabled=lambda: self._automation_enabled,
        )
        # Reactors — zero-token, sub-second responders.
        # Master switch silences everything except Recovery (the
        # watchdog stays alive even when the user takes over).
        self.auto_placer = AutoPlacer(
            self.bus, self.pump.client,
            is_master_enabled=lambda: self._automation_enabled)
        self.stance_nudger = StanceNudger(
            self.bus, self.pump.client,
            is_master_enabled=lambda: self._automation_enabled)
        self.repairer = Repairer(
            self.bus, self.pump.client,
            is_master_enabled=lambda: self._automation_enabled)
        self.recovery = Recovery(
            self.bus, self.pump.client,
            get_active_tasks=self.daemon.active_tasks,
            cancel_task=self.daemon.cancel_task,
            is_build_order_active=self.build_order.is_active,
            build_order_started_at=self.build_order.started_at,
        )
        # Mid-game playbook reactors — codified RA1 macro. They stay
        # silent while BuildOrderRunner is active OR master switch is
        # off, then drive army / economy / defense / pushes
        # deterministically. The LLM is now promoted to a STANDING
        # commander (see Commander) — reactors read PlanStore each tick
        # and follow `army_mix / rally / aggression / tech_next` when a
        # fresh plan exists; otherwise they fall back to hardcoded
        # rotations and heuristics.
        self.plan_store = PlanStore()
        plan_get = self.plan_store.get
        self.army_producer = ArmyProducer(
            self.bus, self.daemon.add_task, self.daemon.snapshot_tasks,
            self.build_order.is_active,
            is_master_enabled=lambda: self._automation_enabled,
            plan_provider=plan_get)
        self.defense_layer = DefenseLayer(
            self.bus, self.daemon.add_task, self.daemon.snapshot_tasks,
            self.build_order.is_active,
            is_master_enabled=lambda: self._automation_enabled)
        self.economy_scaler = EconomyScaler(
            self.bus, self.daemon.add_task, self.daemon.snapshot_tasks,
            self.build_order.is_active,
            is_master_enabled=lambda: self._automation_enabled)
        self.tech_builder = TechBuilder(
            self.bus, self.daemon.add_task, self.daemon.snapshot_tasks,
            self.build_order.is_active,
            is_master_enabled=lambda: self._automation_enabled,
            plan_provider=plan_get)
        self.army_commander = ArmyCommander(
            self.bus, self.pump.client, self.build_order.is_active,
            is_master_enabled=lambda: self._automation_enabled,
            plan_provider=plan_get)
        self.scout = Scout(
            self.bus, self.pump.client, self.build_order.is_active,
            add_task=self.daemon.add_task,
            tasks_provider=self.daemon.snapshot_tasks,
            is_master_enabled=lambda: self._automation_enabled)
        # Commander: standing strategic planner (calls Gemini ~10 s).
        # Reads scout.enemy_quadrant so the LLM knows where waves come from.
        self.commander = Commander(
            bus=self.bus,
            store=self.plan_store,
            is_build_order_active=self.build_order.is_active,
            is_master_enabled=lambda: self._automation_enabled,
            tasks_provider=self.daemon.snapshot_tasks,
            scout_provider=lambda: self.scout.enemy_quadrant,
        )
        # Adviser: consumes events, calls LLM only on real transitions.
        self.adviser = AdviserLoop(
            bus=self.bus,
            add_task=self.daemon.add_task,
            on_advice=self.__onAdvice,
            tasks_provider=self.daemon.snapshot_tasks,
            advisory_enabled=True,
            autopilot_enabled=False,
            build_order_active=self.build_order.is_active,
        )
        self.__build_window()
        # Order matters: subscribers attach BEFORE the pump starts firing,
        # so they don't miss the very first ConnectedEvent / TickEvent.
        self.daemon.start()
        self.build_order.start()
        self.adviser.start()
        self.pump.start()
        return self

    def __build_window(self):
        w, h = 460, 720
        game = find_openra_window()
        if game:
            gx, gy, gw, gh = game
            # Cocoa origin is bottom-left of MAIN screen; CGWindowList y is
            # top-left. We don't know main screen height here without an
            # extra call — for top-right placement compute via screen frame.
            from AppKit import NSScreen
            scr = NSScreen.mainScreen().frame()
            screen_h = scr.size.height
            x = gx + gw - w - 20
            y_top = gy + 20
            y = screen_h - y_top - h
        else:
            x, y = 200, 400

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), style, NSBackingStoreBuffered, False
        )
        win.setTitle_("Vibera")
        win.setLevel_(NSFloatingWindowLevel)
        win.setReleasedWhenClosed_(False)
        win.setMinSize_(NSMakeSize(360, 520))
        self.window = win

        content = win.contentView()
        content.setWantsLayer_(True)
        content.layer().setBackgroundColor_(BG_WIN.CGColor())

        cw = w
        ch = h - 22  # rough; titlebar handled by AppKit
        STATUS_H = 28
        ADVISER_H = 110          # adviser bar (commentary + 3 quick buttons)
        INPUT_H = 78
        chat_h = ch - STATUS_H - ADVISER_H - INPUT_H

        # ---- status row (top) ----
        status = NSView.alloc().initWithFrame_(NSMakeRect(0, ch - STATUS_H, cw, STATUS_H))
        status.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        status.setWantsLayer_(True)
        status.layer().setBackgroundColor_(rgb(17, 20, 26).CGColor())
        content.addSubview_(status)

        title_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(10, 4, 90, 20))
        title_lbl.setStringValue_("●  Commander")
        title_lbl.setBezeled_(False); title_lbl.setDrawsBackground_(False)
        title_lbl.setEditable_(False); title_lbl.setSelectable_(False)
        title_lbl.setTextColor_(ACCENT)
        title_lbl.setFont_(NSFont.boldSystemFontOfSize_(12))
        status.addSubview_(title_lbl)

        # Two toggles in the top-right of the status row: Adviser / Autopilot.
        # Width budget: ~70px each + 10px gap.
        adv_toggle = NSButton.alloc().initWithFrame_(
            NSMakeRect(cw - 240, 4, 75, 20))
        adv_toggle.setButtonType_(NSButtonTypeSwitch)
        adv_toggle.setTitle_("Adviser")
        adv_toggle.setState_(NSControlStateValueOn)
        adv_toggle.setFont_(NSFont.systemFontOfSize_(11))
        adv_toggle.setTarget_(self)
        adv_toggle.setAction_("_toggleAdvisory:")
        # Right-anchor with autoresizing so the button stays glued to the
        # right edge if the user resizes the window.
        from AppKit import NSViewMinXMargin
        adv_toggle.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        status.addSubview_(adv_toggle)
        self.adv_toggle = adv_toggle

        auto_toggle = NSButton.alloc().initWithFrame_(
            NSMakeRect(cw - 160, 4, 75, 20))
        auto_toggle.setButtonType_(NSButtonTypeSwitch)
        auto_toggle.setTitle_("Autopilot")
        auto_toggle.setState_(NSControlStateValueOff)
        auto_toggle.setFont_(NSFont.systemFontOfSize_(11))
        auto_toggle.setTarget_(self)
        auto_toggle.setAction_("_toggleAutopilot:")
        auto_toggle.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        status.addSubview_(auto_toggle)
        self.auto_toggle = auto_toggle

        # Master automation kill switch — silences BO + R1-R5 + placer
        # + nudger + repairer when off (Recovery still runs).
        automation_toggle = NSButton.alloc().initWithFrame_(
            NSMakeRect(cw - 80, 4, 75, 20))
        automation_toggle.setButtonType_(NSButtonTypeSwitch)
        automation_toggle.setTitle_("Auto")
        automation_toggle.setState_(NSControlStateValueOn)
        automation_toggle.setFont_(NSFont.systemFontOfSize_(11))
        automation_toggle.setTarget_(self)
        automation_toggle.setAction_("_toggleAutomation:")
        automation_toggle.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        status.addSubview_(automation_toggle)
        self.automation_toggle = automation_toggle

        self.status_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(105, 4, cw - 355, 20))
        self.status_lbl.setStringValue_("connecting…")
        self.status_lbl.setBezeled_(False); self.status_lbl.setDrawsBackground_(False)
        self.status_lbl.setEditable_(False); self.status_lbl.setSelectable_(False)
        self.status_lbl.setTextColor_(META_FG)
        self.status_lbl.setFont_(NSFont.fontWithName_size_("Menlo", 11) or NSFont.systemFontOfSize_(11))
        self.status_lbl.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        self.status_lbl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        status.addSubview_(self.status_lbl)

        # ---- adviser bar (between status and chat) ----
        adviser_y = ch - STATUS_H - ADVISER_H
        adviser_bar = NSView.alloc().initWithFrame_(
            NSMakeRect(0, adviser_y, cw, ADVISER_H))
        adviser_bar.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        adviser_bar.setWantsLayer_(True)
        adviser_bar.layer().setBackgroundColor_(rgb(22, 26, 33).CGColor())
        content.addSubview_(adviser_bar)

        adv_title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(10, ADVISER_H - 24, 80, 18))
        adv_title.setStringValue_("Adviser")
        adv_title.setBezeled_(False); adv_title.setDrawsBackground_(False)
        adv_title.setEditable_(False); adv_title.setSelectable_(False)
        adv_title.setTextColor_(ACCENT)
        adv_title.setFont_(NSFont.boldSystemFontOfSize_(11))
        adviser_bar.addSubview_(adv_title)

        commentary = NSTextField.alloc().initWithFrame_(
            NSMakeRect(60, ADVISER_H - 24, cw - 70, 18))
        commentary.setStringValue_("Waiting for first suggestion…")
        commentary.setBezeled_(False); commentary.setDrawsBackground_(False)
        commentary.setEditable_(False); commentary.setSelectable_(False)
        commentary.setTextColor_(BOT_FG)
        commentary.setFont_(NSFont.systemFontOfSize_(12))
        commentary.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        commentary.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        adviser_bar.addSubview_(commentary)
        self.adv_commentary_lbl = commentary

        # 3 quick-action buttons in a row.
        btn_w = (cw - 40) // 3
        btn_y = 14
        btn_h = 56
        self.adv_buttons: list = []
        for i in range(3):
            bx = 10 + i * (btn_w + 5)
            btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(bx, btn_y, btn_w, btn_h))
            btn.setButtonType_(NSButtonTypeMomentaryPushIn)
            btn.setBezelStyle_(NSBezelStyleRounded)
            btn.setTitle_("")
            btn.setHidden_(True)
            btn.setFont_(NSFont.systemFontOfSize_(11))
            btn.setTarget_(self)
            btn.setAction_("_quickAction:")
            btn.setTag_(i)
            adviser_bar.addSubview_(btn)
            self.adv_buttons.append(btn)

        # ---- input (bottom) ----
        input_frame = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, cw, INPUT_H))
        input_frame.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(input_frame)

        # Mic button on the right edge — square, height matches input.
        from AppKit import NSViewMinXMargin
        MIC_W = 44
        mic_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(cw - 10 - MIC_W, 10, MIC_W, INPUT_H - 20))
        mic_btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        mic_btn.setBezelStyle_(NSBezelStyleRounded)
        mic_btn.setTitle_("🎙")
        mic_btn.setFont_(NSFont.systemFontOfSize_(18))
        mic_btn.setTarget_(self)
        mic_btn.setAction_("_toggleMic:")
        mic_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        mic_btn.setToolTip_("Click to start recording; click again to stop and transcribe")
        input_frame.addSubview_(mic_btn)
        self.mic_btn = mic_btn
        self._voice_input = None     # lazy — first click constructs
        # Pre-warm the whisper model in a background thread so the first 🎙
        # click doesn't pay 0.5-2s of model-load latency *and* (more importantly)
        # avoids a Metal-init race against OpenRA's GL/Metal context that has
        # previously hung the worker. Best-effort, ignored on failure.
        def _prewarm():
            try:
                from .voice_input import _ensure_model
                _ensure_model()
            except Exception:
                import logging as _l
                _l.getLogger("vibera.voice_input").exception(
                    "VoiceInput: prewarm failed (will retry on first click)")
        threading.Thread(target=_prewarm, daemon=True,
                         name="voice-prewarm").start()

        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(10, 10, cw - 30 - MIC_W, INPUT_H - 20))
        scroll.setBorderType_(0)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        scroll.setDrawsBackground_(True)
        scroll.setBackgroundColor_(INPUT_BG)
        input_frame.addSubview_(scroll)

        tv = CommandTextView.alloc().initWithFrame_(NSMakeRect(0, 0, cw - 30 - MIC_W, INPUT_H - 20))
        tv.setMinSize_(NSMakeSize(0, INPUT_H - 20))
        tv.setMaxSize_(NSMakeSize(1e7, 1e7))
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.setAutoresizingMask_(NSViewWidthSizable)
        tv.setDrawsBackground_(True)
        tv.setBackgroundColor_(INPUT_BG)
        tv.setTextColor_(INPUT_FG)
        tv.setInsertionPointColor_(INPUT_FG)
        tv.setFont_(NSFont.fontWithName_size_("PingFang SC", 13) or NSFont.systemFontOfSize_(13))
        tv.setRichText_(False)
        tc = tv.textContainer()
        tc.setContainerSize_(NSMakeSize(cw - 30 - MIC_W, 1e7))
        tc.setWidthTracksTextView_(True)
        tv.setSubmitHandler_(self.__submitFromUI)
        scroll.setDocumentView_(tv)
        self.input_tv = tv

        # ---- chat scroll (middle) ----
        chat_scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, INPUT_H, cw, chat_h))
        chat_scroll.setBorderType_(0)
        chat_scroll.setHasVerticalScroller_(True)
        chat_scroll.setAutohidesScrollers_(True)
        chat_scroll.setDrawsBackground_(True)
        chat_scroll.setBackgroundColor_(BG_CHAT)
        chat_scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content.addSubview_(chat_scroll)

        chat_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, cw, chat_h))
        chat_view.setMinSize_(NSMakeSize(0, chat_h))
        chat_view.setMaxSize_(NSMakeSize(1e7, 1e7))
        chat_view.setVerticallyResizable_(True)
        chat_view.setHorizontallyResizable_(False)
        chat_view.setEditable_(False)
        chat_view.setSelectable_(True)
        chat_view.setDrawsBackground_(True)
        chat_view.setBackgroundColor_(BG_CHAT)
        chat_view.setTextContainerInset_(NSMakeSize(10, 10))
        chat_view.setRichText_(True)
        chat_view.setAutoresizingMask_(NSViewWidthSizable)
        tc2 = chat_view.textContainer()
        tc2.setContainerSize_(NSMakeSize(cw, 1e7))
        tc2.setWidthTracksTextView_(True)
        chat_scroll.setDocumentView_(chat_view)
        self.chat_view = chat_view
        self.chat_scroll = chat_scroll

        # Focus input
        win.makeFirstResponder_(tv)

        # Greeting
        if game:
            gx, gy, gw, gh = game
            loc = f"OpenRA window @ ({gx},{gy}) {gw}x{gh}"
        else:
            loc = "Tip: launch OpenRA and I'll dock alongside it."
        self.__appendBot(
            "Hi commander. Type a command and I'll dispatch it; the daemon\n"
            "follows up on multi-step tasks.\n"
            "Examples: \"build barracks at a sensible spot\", \"queue 1 heavy\n"
            "tank when we have 1500 cash\", \"pull all infantry back to base\".\n"
            f"{loc}\n"
            "Enter to send · Shift+Enter for newline"
        )
        self.__setStatus_color("daemon starting…", META_FG)

    # ----- chat append helpers -----

    def __appendAttr(self, attr_str):
        storage = self.chat_view.textStorage()
        storage.beginEditing()
        storage.appendAttributedString_(attr_str)
        storage.endEditing()
        # scroll to end
        length = storage.length()
        self.chat_view.scrollRangeToVisible_(NSMakeRange(length, 0))

    def __bubbleAttr(self, text, bg, fg, align):
        # Render a bubble using a paragraph style with background color
        # via NSAttributedString. NSAttributedString supports per-run
        # background color, plus paragraph indent/alignment.
        from AppKit import NSBackgroundColorAttributeName
        para = NSMutableParagraphStyle.alloc().init()
        para.setLineBreakMode_(NSLineBreakByWordWrapping)
        para.setAlignment_(2 if align == "right" else 0)  # right=2, left=0
        para.setParagraphSpacing_(8)
        para.setParagraphSpacingBefore_(4)
        para.setFirstLineHeadIndent_(40 if align == "right" else 8)
        para.setHeadIndent_(40 if align == "right" else 8)
        para.setTailIndent_(-8 if align == "right" else -40)

        font = NSFont.fontWithName_size_("PingFang SC", 12) or NSFont.systemFontOfSize_(12)
        attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: fg,
            NSBackgroundColorAttributeName: bg,
            NSParagraphStyleAttributeName: para,
        }
        # surround with spaces+newlines to give bubble vertical air. The
        # background paints exactly the glyph run, so we pad with " ".
        body = "  " + text.replace("\n", "  \n  ") + "  "
        s = NSAttributedString.alloc().initWithString_attributes_(body + "\n", attrs)
        return s

    def __spacerAttr(self):
        attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(4)}
        return NSAttributedString.alloc().initWithString_attributes_("\n", attrs)

    def __appendUser(self, text):
        self.__appendAttr(self.__bubbleAttr(text, USER_BG, USER_FG, "right"))
        self.__appendAttr(self.__spacerAttr())

    def __appendBot(self, text, is_error=False):
        fg = ERR_FG if is_error else BOT_FG
        self.__appendAttr(self.__bubbleAttr(text, BOT_BG, fg, "left"))
        self.__appendAttr(self.__spacerAttr())

    # ----- submit / worker -----

    def __submitFromUI(self, text):
        # Called on main thread from CommandTextView.keyDown_
        self.__appendUser(text)
        if self.busy:
            self.__appendBot("(busy — queued behind previous command)")
        self.work_q.put(text)
        if not self.busy:
            self.__kickWorker()

    def __kickWorker(self):
        try:
            text = self.work_q.get_nowait()
        except queue.Empty:
            return
        self.busy = True
        self.__setStatus_color("thinking…", ACCENT)
        threading.Thread(target=self.__worker, args=(text,), daemon=True).start()

    def __worker(self, text):
        # New flow:
        #   1. Get a fresh snapshot from the pump (or pull one ourselves
        #      via the pump's shared client if the very first tick hasn't
        #      landed yet — this only happens in the first second after
        #      startup).
        #   2. Translate utterance + lean state -> task plan (LLM).
        #   3. Hand the plan to the daemon as a Task. It executes async.
        # We do NOT open a separate socket here — pump owns the only one,
        # and OpenRAClient.call() is locked so concurrent reads are safe.
        snap = self.pump.last_snapshot()
        if snap is None:
            try:
                snap = self.pump.client.snapshot()
            except Exception as e:
                self.__dispatchResult(("err", f"failed to fetch snapshot: {e}"))
                return

        if snap is None:
            self.__dispatchResult(("err", "cannot reach OpenRA (no recent snapshot from pump)"))
            return

        try:
            state = snapshot_to_lean_state(snap)
            state_json = json.dumps(state, ensure_ascii=False)
            plan = translate_to_plan(text, state_json)
        except Exception as e:
            self.__dispatchResult(("err", f"LLM call failed: {type(e).__name__}: {e}"))
            return

        if plan.get("_error"):
            self.__dispatchResult(("plan", plan))
            return

        conf = plan.get("confidence") or 0
        if conf < 0.5:
            plan["_skipped_reason"] = f"confidence too low ({conf}); skipping"
            self.__dispatchResult(("plan", plan))
            return

        if not plan.get("steps"):
            plan["_skipped_reason"] = "LLM returned no steps"
            self.__dispatchResult(("plan", plan))
            return

        # Display the plan first (so the user sees what the LLM proposed),
        # then hand it off to the daemon. Subsequent task-state changes
        # arrive via __onDaemonChange.
        task = build_task(text, plan)
        self.daemon.add_task(task)
        self.__dispatchResult(("plan_ok", (plan, task.id)))

    def __dispatchResult(self, payload):
        # bounce to main thread
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "_handleResult:", payload, False
        )

    def _handleResult_(self, payload):
        kind, data = payload
        if kind == "err":
            self.__appendBot(str(data), is_error=True)
        elif kind == "plan":
            text, is_err = fmt_plan(data)
            if data.get("_skipped_reason"):
                text += f"\n⚠ {data['_skipped_reason']}"
                is_err = True
            self.__appendBot(text, is_error=is_err)
        elif kind == "plan_ok":
            plan, task_id = data
            text, is_err = fmt_plan(plan)
            text = f"[{task_id}] " + text
            self.__appendBot(text, is_error=is_err)
        elif kind == "tasks":
            for line, is_err in data:
                self.__appendBot(line, is_error=is_err)
            return  # daemon-initiated; do NOT touch busy/work_q
        self.busy = False
        self.__setStatus_color("ready", META_FG)
        if not self.work_q.empty():
            self.__kickWorker()

    # ----- status -----

    def __setStatus_color(self, text, color):
        def _do():
            self.status_lbl.setStringValue_(text)
            self.status_lbl.setTextColor_(color)
        # if called from non-main, marshal
        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "_setStatusMain:", (text, color), False
            )

    def _setStatusMain_(self, args):
        text, color = args
        self.status_lbl.setStringValue_(text)
        self.status_lbl.setTextColor_(color)

    # ----- adviser bar -----

    def _toggleAdvisory_(self, sender):
        on = sender.state() == NSControlStateValueOn
        self.adviser.set_advisory(on)
        if not on:
            # Clear the bar so a stale suggestion doesn't sit there.
            self.adv_commentary_lbl.setStringValue_("(adviser off)")
            for btn in self.adv_buttons:
                btn.setHidden_(True)

    def _toggleAutopilot_(self, sender):
        on = sender.state() == NSControlStateValueOn
        self.adviser.set_autopilot(on)
        # Visual confirmation in chat so the user sees the mode change.
        self.__appendBot(
            f"{'⚙ Autopilot ON — high-confidence suggestions auto-execute' if on else '⚙ Autopilot OFF'}",
            is_error=False,
        )

    def _toggleAutomation_(self, sender):
        on = sender.state() == NSControlStateValueOn
        # Plain attribute write — readers grab a fresh value via lambda
        # at every event, so no lock needed for monotonic flips.
        self._automation_enabled = on
        self.__appendBot(
            f"{'⚙ Auto ON — build-order + reactors will run the base' if on else '⚙ Auto OFF — you drive (Recovery still active)'}",
            is_error=False,
        )

    # ----- voice (mic) -----

    def _toggleMic_(self, sender):
        # First click → start recording. Second click → stop + transcribe
        # in a worker thread (whisper.cpp is sync and we don't want to
        # block the AppKit run loop). Lazy-construct VoiceInput on first
        # use so unrelated runs don't pay the import / model-load cost.
        if self._voice_input is None:
            try:
                from .voice_input import VoiceInput
                self._voice_input = VoiceInput()
            except Exception as e:
                self.__appendBot(f"🎙 voice_input load failed: {e}", is_error=True)
                return

        vi = self._voice_input
        if not vi.recording:
            try:
                vi.start()
            except Exception as e:
                self.__appendBot(f"🎙 recording failed to start: {e}", is_error=True)
                return
            self.mic_btn.setTitle_("⏺")
            self.__setStatus_color("🎙 recording… click again to stop", ACCENT)
            return

        # stop + transcribe in a worker so UI stays responsive
        self.mic_btn.setTitle_("…")
        self.mic_btn.setEnabled_(False)
        self.__setStatus_color("🎙 transcribing…", ACCENT)
        threading.Thread(
            target=self.__voiceWorker, daemon=True,
            name="voice-transcribe").start()

    def __voiceWorker(self):
        text = ""
        err: Optional[str] = None
        try:
            text = self._voice_input.stop_and_transcribe()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        # Stash on self because performSelectorOnMainThread bridges Python
        # objects through Cocoa, which mangles tuples/None into NSArray/
        # NSNull. Plain attribute access keeps types intact.
        self._voice_pending = (text or "", err)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "_handleVoiceResult:", None, False)

    def _handleVoiceResult_(self, _ignored):
        text, err = getattr(self, "_voice_pending", ("", None))
        self._voice_pending = None
        self.mic_btn.setEnabled_(True)
        self.mic_btn.setTitle_("🎙")
        self.__setStatus_color("idle", MUTED)
        if err:
            self.__appendBot(f"🎙 transcribe failed: {err}", is_error=True)
            return
        if not text:
            self.__appendBot("🎙 nothing recognised (too short / too quiet / silent?)", is_error=True)
            return
        # Auto-submit — the whole point is hands-off. User can edit + re-send
        # if the transcription was wrong.
        self.__submitFromUI(text)

    def _quickAction_(self, sender):
        idx = sender.tag()
        if idx < 0 or idx >= len(self.adviser_suggestions):
            return
        sug = self.adviser_suggestions[idx]
        plan = sug.get("task_plan") or {}
        title = sug.get("title") or "quick action"
        try:
            task = Task.new(
                intent=str(plan.get("intent") or title),
                steps=plan.get("steps") or [],
                utterance=f"<quick action: {title}>",
            )
            self.daemon.add_task(task)
            self.__appendBot(f"▶ {title}", is_error=False)
        except Exception as e:
            self.__appendBot(f"quick action failed: {e}", is_error=True)
        # Hide the row so the user doesn't double-click; next adviser tick
        # will repopulate.
        sender.setHidden_(True)

    def __onAdvice(self, advice: dict):
        """Background-thread callback from AdviserLoop. Marshal to main."""
        try:
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "_renderAdviceMain:", advice, False
            )
        except Exception:
            pass

    def _renderAdviceMain_(self, advice):
        if "_error" in advice:
            # JSON parse / network blip — keep last commentary visible to
            # avoid the "adviser offline" flash that the operator can't act on.
            # Log the error (already done in adviser.py); just don't
            # repaint the label. Fade the colour so the user can tell.
            try:
                # Drop suggestion rows since they were tied to the prior
                # successful advice and may no longer be relevant.
                for btn in self.adv_buttons:
                    btn.setHidden_(True)
                self.adviser_suggestions = []
            except Exception:
                pass
            return

        commentary = advice.get("commentary") or ""
        latency = advice.get("_latency_sec")
        if latency:
            commentary = f"{commentary}  ({latency}s)"
        self.adv_commentary_lbl.setStringValue_(commentary or "(no commentary)")
        self.adv_commentary_lbl.setTextColor_(BOT_FG)

        suggestions = advice.get("suggestions") or []
        self.adviser_suggestions = suggestions
        for i, btn in enumerate(self.adv_buttons):
            if i < len(suggestions):
                sug = suggestions[i]
                conf = sug.get("confidence", "low")
                title = sug.get("title", "?")
                reason = sug.get("reason", "")
                # 2-line label: title + confidence/reason (truncated).
                glyph = {"high": "★", "med": "◆", "low": "·"}.get(conf, "·")
                short = (reason[:28] + "…") if len(reason) > 30 else reason
                btn.setTitle_(f"{glyph} {title}\n{short}")
                btn.setToolTip_(f"[{conf}] {reason}")
                # PyObjC doesn't expose easy multi-line cells; setting
                # title with \n works on standard buttons because we use
                # NSBezelStyleRounded which auto-wraps in 10.12+.
                btn.setHidden_(False)
            else:
                btn.setTitle_("")
                btn.setHidden_(True)

    # ----- daemon callback (background thread) -----

    def __onDaemonChange(self):
        """Called by TaskDaemon whenever a task list changes. Diff against
        our last view; render one bubble per task whose (state, cursor)
        moved since we last looked."""
        try:
            tasks = self.daemon.snapshot_tasks()
            changed: list[tuple[str, bool]] = []
            with self.task_view_lock:
                seen = self.task_view
                new_view: dict[str, tuple[str, int]] = {}
                for t in tasks:
                    key = (t.state, t.cursor)
                    new_view[t.id] = key
                    if seen.get(t.id) != key:
                        line, is_err = fmt_task_change(t)
                        changed.append((line, is_err))
                self.task_view = new_view
            # Update status bar with rough live snapshot info. Pull from
            # the pump (source of truth) rather than the daemon — pump
            # updates every second; daemon's copy may lag while it's busy
            # executing a task.
            snap = self.pump.last_snapshot()
            if snap:
                active = sum(1 for t in tasks if not t.is_terminal)
                cash = snap.self_state.cash if snap.self_state else 0
                self.__setStatus_color(
                    f"tick {snap.tick} · ${cash} · {active} active task(s)",
                    META_FG,
                )
            elif self.pump.last_error():
                self.__setStatus_color(
                    f"pump: {self.pump.last_error()}", ERR_FG
                )
            if changed:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "_handleResult:", ("tasks", changed), False
                )
        except Exception:
            # Never let a render error kill the daemon thread.
            pass

    # ----- show -----

    def show(self):
        self.window.makeKeyAndOrderFront_(None)


# Bind ObjC selectors with proper signatures (PyObjC needs this when the
# Python-side method takes a Python tuple). For our use, plain method
# names work because performSelectorOnMainThread passes one object.
ChatController._handleResult_ = objc.selector(
    ChatController._handleResult_, signature=b"v@:@"
)
ChatController._setStatusMain_ = objc.selector(
    ChatController._setStatusMain_, signature=b"v@:@"
)
ChatController._renderAdviceMain_ = objc.selector(
    ChatController._renderAdviceMain_, signature=b"v@:@"
)
ChatController._toggleAdvisory_ = objc.selector(
    ChatController._toggleAdvisory_, signature=b"v@:@"
)
ChatController._toggleAutopilot_ = objc.selector(
    ChatController._toggleAutopilot_, signature=b"v@:@"
)
ChatController._toggleAutomation_ = objc.selector(
    ChatController._toggleAutomation_, signature=b"v@:@"
)
ChatController._quickAction_ = objc.selector(
    ChatController._quickAction_, signature=b"v@:@"
)


def main():
    from . import config
    host = config.OPENRA_HOST
    port = config.OPENRA_PORT
    if not os.environ.get("GEMINI_API_KEY"):
        print("warning: GEMINI_API_KEY not set; LLM calls will fail.",
              file=sys.stderr)

    NSApplication.sharedApplication()
    ctrl = ChatController.alloc().initWithHost_port_(host, port)
    ctrl.show()
    NSApp().activateIgnoringOtherApps_(True)
    NSApp().run()


if __name__ == "__main__":
    main()
