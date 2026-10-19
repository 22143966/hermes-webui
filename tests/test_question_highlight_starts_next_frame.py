"""The jump highlight starts on the frame after the jump (#5106).

A far jump renders and lays out the whole transcript in the same task as the scroll. A CSS animation started inside
that task takes its clock from the frame that began before it, so on a 240-message session the first painted frame
was already 0.4-0.6 s into the 1.6 s pulse. ``_highlightQuestionRow`` now adds the class on the next animation frame.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UI_JS = REPO / "static" / "ui.js"
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
function extract(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw Error('missing ' + name);
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth && i < src.length) { if (src[i] === '{') depth++; if (src[i] === '}') depth--; i++; }
  return src.slice(start, i);
}
const timers = new Map(); let nextTimer = 1;
global.window = global;
window.setTimeout = (f, ms) => { const id = nextTimer++; timers.set(id, {f, ms}); return id; };
window.clearTimeout = (id) => { timers.delete(id); };
const frames = [];
if (scenario !== 'no-raf') global.requestAnimationFrame = (f) => { frames.push(f); return frames.length; };
const classes = new Set(); let reflows = 0;
const row = {
  classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
  get offsetWidth() { reflows++; return 100; },
};
eval(extract('_highlightQuestionRow'));
const has = () => classes.has('msg-question-highlight');
const flush = () => { while (frames.length) frames.shift()(); };
const fire = () => { const all = [...timers.entries()]; timers.clear(); for (const [, t] of all) t.f(); };
const out = {};
_highlightQuestionRow(row);
out.sync = has();
out.pendingFrames = frames.length;
flush();
out.afterFrame = has();
out.reflowsBeforeAdd = reflows;
out.timerMs = [...timers.values()].map(t => t.ms);
if (scenario === 'repeat') {
  // A second jump to the same row while the first highlight is still on: the first removal timer must not
  // cut the second highlight short.
  _highlightQuestionRow(row);
  out.timersAfterRepeat = timers.size;
  flush();
  out.timersAfterRepeatFrame = timers.size;
  out.afterRepeatFrame = has();
}
fire();
out.afterTimer = has();
_highlightQuestionRow(null);
out.nullFrames = frames.length;
console.log(JSON.stringify(out));
"""


def _run(scenario="default"):
    result = subprocess.run([NODE, "-e", _DRIVER, str(UI_JS), scenario], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_highlight_class_is_added_on_the_next_frame_not_inside_the_jump_task():
    r = _run()
    assert r["sync"] is False, "the class must not be added inside the task that rendered and scrolled"
    assert r["pendingFrames"] == 1
    assert r["afterFrame"] is True
    assert r["reflowsBeforeAdd"] == 1, "the class is re-added after a reflow so the pulse restarts"
    assert r["timerMs"] == [1800]
    assert r["afterTimer"] is False
    assert r["nullFrames"] == 0


@needs_node
def test_repeat_jump_keeps_the_second_highlight_for_its_full_duration():
    r = _run("repeat")
    assert r["timersAfterRepeat"] == 0, "the first highlight's removal timer is cancelled"
    assert r["timersAfterRepeatFrame"] == 1
    assert r["afterRepeatFrame"] is True
    assert r["afterTimer"] is False


@needs_node
def test_without_request_animation_frame_the_highlight_is_applied_immediately():
    r = _run("no-raf")
    assert r["sync"] is True
    assert r["afterTimer"] is False


def test_reduced_motion_keeps_a_static_ring_instead_of_the_pulse():
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{\s*\.msg-question-highlight \.msg-body \{([^}]*)\}",
        STYLE_CSS,
    )
    assert block, "reduced-motion override for the jump highlight is missing"
    body = block.group(1)
    assert "animation: none;" in body
    assert "box-shadow: 0 0 0 3px var(--focus-ring);" in body
