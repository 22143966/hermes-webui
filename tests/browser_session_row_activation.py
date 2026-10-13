#!/usr/bin/env python3
"""
Headless browser gate: a sidebar conversation row opens from the keyboard and
from a screen reader's activate command, not only from a pointer press.

WHY THIS EXISTS
  A session row opened only from its pointerdown/pointerup (or touchstart/
  touchend) gesture handlers, so swipe, long-press and double-tap rename could
  share one state machine. The row had no role, no tabindex and no click
  handler. NVDA in Firefox activates an element by dispatching mousedown,
  mouseup and click with no pointer events before them, and Enter on a
  focusable element dispatches a bare click, so neither could open a
  conversation; VoiceOver in Safari happened to work because it synthesizes
  pointer events as well.

WHAT IT CHECKS (Chromium, and Firefox when its Playwright build is installed)
  - every row title is a focusable link; the open conversation's title carries
    aria-current="page", the others do not;
  - mouse events without pointer events (how Firefox performs an assistive
    technology's activate action) open the conversation, once;
  - a bare element.click() opens the conversation;
  - Tab focus on a title plus Enter opens it;
  - a sidebar repaint never moves focus to a title (a screen reader's reading
    cursor would be pulled back to the list);
  - a real mouse click still opens the conversation exactly once;
  - Enter on a row's actions trigger opens its menu and does not open the
    conversation;
  - in selection mode the title is not a link (the row checkbox is the control).
  touch (Chromium, 390x844, drawer open)
  - a tap still opens the conversation exactly once.

  --screenshots DIR writes the sidebar at 1440x900 and 390x844 (drawer open)
  after pressing Tab from the conversation filter until focus reaches the
  second conversation's title (or 25 presses), showing where keyboard focus
  lands.
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = int(os.getenv("SESSION_ROW_ACTIVATION_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"
WAIT_MS = 5000
# Opening a conversation repaints the sidebar again shortly after; wait it out
# before counting opens or reading focus.
SETTLE_MS = 800


def _wait_for_health(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _wait_until(page, expression, arg=None, timeout_ms=WAIT_MS):
    """Poll ``expression`` with page.evaluate. The app's CSP has no 'unsafe-eval',
    which Playwright's interval-polled wait_for_function needs."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression, arg):
            return True
        page.wait_for_timeout(50)
    return False


SEED_JS = """async () => {
  const post = async (path, body) => {
    const response = await fetch(path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(path + ' failed: ' + JSON.stringify(data));
    return data;
  };
  const messages = [
    {role: 'user', content: 'hello'},
    {role: 'assistant', content: 'hello back'},
  ];
  const sids = [];
  for (const title of ['Alpha conversation', 'Beta conversation']) {
    const data = await post('/api/session/import', {title, messages});
    sids.push(data.session.session_id);
  }
  await renderSessionList({deferWhileInteracting: false});
  return {alpha: sids[0], beta: sids[1]};
}"""

# Count every sidebar open. _openSidebarSession is a global function the row
# handlers call by name, so replacing the global binding sees all of them.
COUNT_OPENS_JS = """() => {
  if (!window.__sidebarOpens) {
    window.__sidebarOpens = [];
    const original = _openSidebarSession;
    window._openSidebarSession = async (session, opts) => {
      window.__sidebarOpens.push(session && session.session_id);
      return original(session, opts);
    };
  }
  window.__sidebarOpens.length = 0;
}"""

ACTIVE_JS = "(sid) => !!(S.session && S.session.session_id === sid)"

TITLE_SELECTOR_JS = """(sid) => {
  const row = Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
    .find(node => node.dataset.sid === sid);
  return row ? row.querySelector('.session-title') : null;
}"""

TITLES_JS = """() => Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]')).map(row => {
  const title = row.querySelector('.session-title');
  return {
    sid: row.dataset.sid,
    role: title && title.getAttribute('role'),
    tabindex: title && title.getAttribute('tabindex'),
    current: title && title.getAttribute('aria-current'),
  };
})"""

FOCUSED_SID_JS = """() => {
  const active = document.activeElement;
  if (!active || !active.classList.contains('session-title') || !active.isConnected) return null;
  const row = active.closest('.session-item');
  return row ? row.dataset.sid : null;
}"""


def _title(page, sid):
    handle = page.evaluate_handle(TITLE_SELECTOR_JS, sid)
    return handle.as_element()


def _open(page, sid):
    page.evaluate("(sid) => loadSession(sid)", sid)
    _wait_until(page, ACTIVE_JS, sid)
    page.wait_for_timeout(SETTLE_MS)


def _opens(page):
    return page.evaluate("() => window.__sidebarOpens.slice()")


def _new_page(browser, **context_args):
    ctx = browser.new_context(base_url=BASE, **context_args)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=15000)
    ready = "() => typeof S !== 'undefined' && typeof renderSessionList === 'function'"
    if not _wait_until(page, ready, timeout_ms=15000):
        ctx.close()
        return None, None, ["the app did not initialize"]
    page.wait_for_timeout(1000)
    return ctx, page, errors


def _check_desktop(page, seed, name):
    failures = []
    alpha, beta = seed["alpha"], seed["beta"]

    def fail(message):
        failures.append(f"  [{name}] {message}")

    _open(page, alpha)
    page.evaluate(COUNT_OPENS_JS)

    titles = {t["sid"]: t for t in page.evaluate(TITLES_JS)}
    for sid in (alpha, beta):
        t = titles.get(sid)
        if not t:
            fail(f"row {sid} missing from the sidebar")
            return failures
        if t["role"] != "link" or t["tabindex"] != "0":
            fail(f"title of {sid} is not a focusable link: {t}")
    if titles[alpha]["current"] != "page":
        fail(f"open conversation's title lacks aria-current=page: {titles[alpha]}")
    if titles[beta]["current"] is not None:
        fail(f"closed conversation's title carries aria-current: {titles[beta]}")

    # How Firefox performs an assistive technology's activate action: mouse
    # events, no pointer events.
    page.evaluate("""(el) => {
      for (const type of ['mousedown', 'mouseup', 'click']) {
        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window, detail: 1, button: 0}));
      }
    }""", _title(page, beta))
    if not _wait_until(page, ACTIVE_JS, beta):
        fail("mouse events without pointer events did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [beta]:
        fail(f"assistive activation opened {_opens(page)}, expected exactly [beta]")

    _open(page, beta)
    page.evaluate(COUNT_OPENS_JS)
    page.evaluate("(el) => el.click()", _title(page, alpha))
    if not _wait_until(page, ACTIVE_JS, alpha):
        fail("a bare click() did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)

    _open(page, alpha)
    page.evaluate(COUNT_OPENS_JS)
    _title(page, beta).focus()
    page.keyboard.press("Enter")
    if not _wait_until(page, ACTIVE_JS, beta):
        fail("Enter on a focused title did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [beta]:
        fail(f"Enter opened {_opens(page)}, expected exactly [beta]")
    current = {t["sid"]: t["current"] for t in page.evaluate(TITLES_JS)}
    if current.get(beta) != "page" or current.get(alpha) is not None:
        fail(f"aria-current did not follow the open conversation: {current}")

    # A screen reader leaves the browser focus on the activated title while its
    # reading cursor moves through the conversation. A background repaint must
    # not focus anything: NVDA would pull the reading cursor back to the list.
    _title(page, alpha).focus()
    page.evaluate("""() => {
      window.__titleFocusEvents = 0;
      if (!window.__titleFocusListener) {
        window.__titleFocusListener = true;
        document.addEventListener('focusin', (e) => {
          if (e.target && e.target.classList && e.target.classList.contains('session-title')) window.__titleFocusEvents++;
        }, true);
      }
    }""")
    page.evaluate("() => { renderSessionListFromCache(); renderSessionListFromCache(); }")
    page.wait_for_timeout(SETTLE_MS)
    if page.evaluate("() => window.__titleFocusEvents"):
        fail("a background sidebar repaint moved focus to a conversation title")

    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    _open(page, beta)
    page.mouse.move(0, 0)
    page.evaluate(COUNT_OPENS_JS)
    _title(page, alpha).click()
    if not _wait_until(page, ACTIVE_JS, alpha):
        fail("a mouse click no longer opens the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [alpha]:
        fail(f"a mouse click opened {_opens(page)}, expected exactly [alpha]")

    _open(page, alpha)
    page.evaluate(COUNT_OPENS_JS)
    trigger = page.evaluate_handle("""(sid) => {
      const row = Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
        .find(node => node.dataset.sid === sid);
      return row && row.querySelector('.session-actions-trigger');
    }""", beta).as_element()
    trigger.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
        fail("Enter on the actions trigger did not open its menu")
    if _opens(page) or not page.evaluate(ACTIVE_JS, alpha):
        fail(f"Enter on the actions trigger opened a conversation: {_opens(page)}")
    page.keyboard.press("Escape")
    page.evaluate("() => typeof closeSessionActionMenu === 'function' && closeSessionActionMenu()")
    page.wait_for_timeout(300)

    page.evaluate("() => toggleSessionSelectMode()")
    page.wait_for_timeout(300)
    roles = [t["role"] for t in page.evaluate(TITLES_JS)]
    if any(roles):
        fail(f"selection mode still exposes titles as links: {roles}")
    page.evaluate("() => exitSessionSelectMode()")
    page.wait_for_timeout(300)
    return failures


def _check_touch(page, seed):
    failures = []
    alpha, beta = seed["alpha"], seed["beta"]
    _open(page, alpha)
    page.evaluate(
        "() => { if (typeof toggleMobileSidebar === 'function'"
        " && !document.querySelector('.sidebar.mobile-open')) toggleMobileSidebar(); }"
    )
    page.wait_for_timeout(400)
    page.evaluate(COUNT_OPENS_JS)
    _title(page, beta).tap()
    if not _wait_until(page, ACTIVE_JS, beta):
        failures.append("  [touch] a tap no longer opens the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [beta]:
        failures.append(f"  [touch] a tap opened {_opens(page)}, expected exactly [beta]")
    return failures


def _screenshots(browser, directory, seed):
    os.makedirs(directory, exist_ok=True)
    for width, height, touch in ((1440, 900, False), (390, 844, True)):
        extra = {"has_touch": True, "is_mobile": True} if touch else {}
        ctx, page, _errors = _new_page(browser, viewport={"width": width, "height": height}, **extra)
        if page is None:
            continue
        _open(page, seed["alpha"])
        if touch:
            page.evaluate(
                "() => { if (typeof toggleMobileSidebar === 'function'"
                " && !document.querySelector('.sidebar.mobile-open')) toggleMobileSidebar(); }"
            )
            page.wait_for_timeout(400)
        page.focus("#sessionSearch")
        for _ in range(25):
            page.keyboard.press("Tab")
            if page.evaluate(FOCUSED_SID_JS) == seed["beta"]:
                break
        page.wait_for_timeout(200)
        path = os.path.join(directory, f"session-row-focus-{width}x{height}.png")
        page.screenshot(path=path)
        print(f"shot {path}")
        ctx.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    shots = None
    if "--screenshots" in sys.argv:
        shots = sys.argv[sys.argv.index("--screenshots") + 1]

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-session-row-activation-")
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        seed = None
        with sync_playwright() as pw:
            engines = [("chromium", pw.chromium, {"args": ["--no-sandbox", "--disable-dev-shm-usage"]})]
            if os.path.exists(pw.firefox.executable_path):
                engines.append(("firefox", pw.firefox, {}))
            else:
                print("NOTE firefox: Playwright's Firefox is not installed; checking Chromium only")
            for name, engine, launch_args in engines:
                browser = engine.launch(headless=True, **launch_args)
                ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
                if page is None:
                    failures.append(f"  [{name}] {errors}")
                    browser.close()
                    continue
                if seed is None:
                    seed = page.evaluate(SEED_JS)
                else:
                    page.evaluate("() => renderSessionList({deferWhileInteracting: false})")
                    page.wait_for_timeout(500)
                found = _check_desktop(page, seed, name)
                failures.extend(found)
                if not found:
                    print(f"OK  {name} — rows open from assistive activation, click(), Enter and the mouse; repaints never steal focus")
                failures.extend(f"  [{name}] pageerror: {err}" for err in errors)
                ctx.close()

                if name == "chromium":
                    ctx, page, errors = _new_page(
                        browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
                    )
                    if page is None:
                        failures.append(f"  [touch] {errors}")
                    else:
                        found = _check_touch(page, seed)
                        failures.extend(found)
                        if not found:
                            print("OK  touch 390x844 — a tap opens the conversation once")
                        failures.extend(f"  [touch] pageerror: {err}" for err in errors)
                        ctx.close()
                    if shots and seed:
                        _screenshots(browser, shots, seed)
                browser.close()

        if failures:
            print("\nSESSION ROW ACTIVATION FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nSESSION ROW ACTIVATION PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
