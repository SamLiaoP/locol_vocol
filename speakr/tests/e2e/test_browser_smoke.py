"""Browser smoke test: every page loads and its header works, in a real browser.

The API-level tests never render a page, so two header regressions reached a
release: a Vue template error (an element with both v-text and fallback text)
that left the header blank, and a request for vendor/js/vue.global.prod.js, a
file the image does not contain, which left the settings-page headers empty.
This test logs in through the login form, opens each page in headless
Chromium, and fails on:

- any same-origin response with status 400 or higher;
- any JavaScript error (uncaught exception or console error);
- template text left in the page (``${`` or ``{{``);
- a header that is not mounted, or a user menu that does not open with its
  entries;
- the Tailwind Play CDN build (vendor/js/tailwind.min.js or
  cdn.tailwindcss.com) requested, or its "should not be used in production"
  console warning; the stylesheet is compiled at build time instead;
- Tailwind utilities from static/css/tailwind.css not applied;
- translation keys (for example ``nav.account``) shown as text in the header.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL (the test.yml browser-smoke job starts one from source with
tests/e2e/run_speakr.py; docker-publish.yml runs the built image before it is
pushed). SPEAKR_SMOKE_USER / SPEAKR_SMOKE_PASSWORD default to the admin in
config/env.whisperx.example.

Console messages that are not failures (documented allowlist):
- messages from browser extensions (chrome-extension://);
- "Failed to load resource": the browser's echo of a failed response, which
  is already reported with its URL by the response check.
"""

import os
import json
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser smoke test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
USER = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))

MENU_TARGETS = ("a[href='/people']", "a[href='/account']", "a[href='/references']", "form[action='/logout'] button")
IGNORED_CONSOLE = (re.compile(r"chrome-extension://"), re.compile(r"Failed to load resource"))
PLAY_CDN = re.compile(r"tailwind\.min\.js|cdn\.tailwindcss\.com")
# A translation key: two or more dot-separated identifiers, the first one
# lower case (nav.account, inquire.title, tokenBudget.percentage).
TRANSLATION_KEY = re.compile(r"\b[a-z][a-zA-Z]*(?:\.[a-zA-Z]+)+\b")


class PageProblems:
    """Collects failed responses and JavaScript errors for the current page."""

    def __init__(self, page):
        self.failed, self.errors, self.play_cdn = [], [], []
        page.on("request", self._request)
        page.on("response", self._response)
        page.on("pageerror", lambda exc: self.errors.append(f"uncaught: {exc}"))
        page.on("console", self._console)

    def _request(self, request):
        if PLAY_CDN.search(request.url):
            self.play_cdn.append(f"request: {request.url}")

    def _response(self, response):
        if response.url.startswith(BASE) and response.status >= 400:
            self.failed.append(f"{response.status} {response.url}")

    def _console(self, msg):
        text = msg.text
        if "cdn.tailwindcss.com" in text:
            self.play_cdn.append(f"console {msg.type}: {text}")
        if msg.type != "error":
            return
        location = (msg.location or {}).get("url", "")
        if any(p.search(text) or p.search(location) for p in IGNORED_CONSOLE):
            return
        self.errors.append(f"console: {text}")

    def reset(self):
        self.failed.clear()
        self.errors.clear()
        self.play_cdn.clear()


@pytest.fixture(scope="module")
def browser_page():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        page = context.new_page()
        problems = PageProblems(page)
        page.goto(f"{BASE}/login")
        page.fill("input[name=email]", USER)
        page.fill("input[name=password]", PASSWORD)
        page.locator("form:has(input[name=password]) [type=submit]:visible").first.click()
        page.wait_for_load_state("load")
        assert "/login" not in page.url, f"could not log in as {USER}"
        problems.reset()
        yield page, problems
        context.close()
        browser.close()


def _open(page, problems, path):
    # Leave the previous page first: its requests still in flight are
    # aborted on unload and logged as "Failed to fetch", which belongs to
    # that page, not the one being checked.
    page.goto("about:blank")
    page.wait_for_timeout(300)
    problems.reset()
    response = page.goto(f"{BASE}{path}")
    page.wait_for_load_state("load")
    # Headers mount after the page's scripts run; give polling apps a moment.
    page.wait_for_timeout(2500)
    return response


def _check_common(page, problems, path):
    body = "\n".join(frame.inner_text("body") for frame in page.frames)
    leftovers = [m for m in ("${", "{{") if m in body]
    assert not leftovers, f"{path}: template text left in the page: {leftovers}"
    assert not problems.failed, f"{path}: failed requests: {problems.failed}"
    assert not problems.errors, f"{path}: JavaScript errors: {problems.errors}"
    assert not problems.play_cdn, f"{path}: Tailwind Play CDN in use: {problems.play_cdn}"
    # Utilities from the compiled stylesheet apply (a probe element, so the
    # check does not depend on any page's markup).
    probe = page.evaluate("""() => {
        const el = document.createElement('div');
        el.className = 'rounded-lg px-4 hidden';
        document.body.appendChild(el);
        const s = getComputedStyle(el);
        const out = {display: s.display, radius: s.borderTopLeftRadius, padding: s.paddingLeft};
        el.remove();
        return out;
    }""")
    assert probe == {"display": "none", "radius": "8px", "padding": "16px"}, f"{path}: Tailwind utilities not applied: {probe}"


def _check_header_translated(page, path, selector):
    header = page.locator(selector).first
    assert header.count() == 1, f"{path}: no header ({selector})"
    keys = TRANSLATION_KEY.findall(header.inner_text())
    assert not keys, f"{path}: translation keys shown in the header: {keys}"


def _check_user_menu(page, path):
    toggle = page.locator("[data-user-menu-toggle]:visible").first
    assert toggle.count() == 1, f"{path}: no user menu button in the header"
    toggle.click()
    menu = page.locator("[data-user-menu-dropdown]:visible").first
    menu.wait_for(state="visible", timeout=5000)
    missing = [selector for selector in MENU_TARGETS if menu.locator(selector).count() != 1]
    assert not missing, f"{path}: user menu is missing {missing}"
    page.keyboard.press("Escape")
    page.mouse.click(5, 400)


def test_main_view(browser_page):
    page, problems = browser_page
    _open(page, problems, "/")
    assert page.locator("[data-v-app]").count() >= 1, "/: the app did not mount"
    _check_header_translated(page, "/", "header")
    _check_user_menu(page, "/")
    _check_common(page, problems, "/")


@pytest.mark.parametrize("path", ["/account", "/admin"])
def test_settings_pages(browser_page, path):
    page, problems = browser_page
    response = _open(page, problems, path)
    assert response is not None and response.status == 200, f"{path}: HTTP {response and response.status}"
    header = page.locator("#app header").first
    assert header.count() == 1, f"{path}: no shared page header"
    assert page.locator("#app[data-v-app]").count() == 1, f"{path}: the workspace did not mount"
    assert header.locator("[data-recordings-toggle]").is_visible()
    _check_header_translated(page, path, "#app header")
    _check_user_menu(page, path)
    if path == "/account":
        tabs = page.frame_locator(".lv-workspace-frame").locator(".tabs button:visible, .tabs a:visible")
        assert tabs.count() == 2, "/account: the tab strip did not render"
    _check_common(page, problems, path)


def test_inquire_page(browser_page):
    page, problems = browser_page
    response = _open(page, problems, "/inquire")
    assert response is not None and response.status == 200
    if "/inquire" not in page.url:
        # Disabled in Locol-Vocol: the route deliberately returns to recordings.
        assert page.url.rstrip('/') == BASE
        _check_common(page, problems, '/inquire (disabled redirect)')
        return
    problems.failed[:] = [f for f in problems.failed if "/inquire " not in f]
    assert page.frame_locator(".lv-workspace-frame").locator("#inquire-app[data-v-app]").count() == 1, "/inquire: the app did not mount"
    _check_header_translated(page, "/inquire", "#app header")
    _check_user_menu(page, "/inquire")
    _check_common(page, problems, "/inquire")


def test_a_recording_detail_view(browser_page):
    page, problems = browser_page
    listing = page.request.get(f"{BASE}/api/recordings?per_page=1")
    items = listing.json().get("recordings", []) if listing.ok else []
    if not items:
        pytest.skip("no recordings on this instance")
    path = f"/recordings/{items[0]['id']}"
    _open(page, problems, path)
    _check_user_menu(page, path)
    _check_common(page, problems, path)


def test_removed_chat_does_not_appear_on_recording_pages(browser_page):
    page, problems = browser_page
    listing = page.request.get(f"{BASE}/api/recordings?per_page=1")
    items = listing.json().get("recordings", []) if listing.ok else []

    if not items:
        pytest.skip('No recordings; the empty library is covered by test_main_view')
    _open(page, problems, f"/recordings/{items[0]['id']}")
    assert page.locator('.floating-chat-fab:visible').count() == 0
    _check_common(page, problems, 'recording without removed chat')


@pytest.mark.parametrize("path", ["/people", "/references"])
def test_product_pages(browser_page, path):
    page, problems = browser_page
    response = _open(page, problems, path)
    assert response is not None and response.status == 200
    assert page.frame_locator('.lv-workspace-frame').locator('h1:visible, h2:visible').count() >= 1
    _check_common(page, problems, path)


@pytest.mark.parametrize("path", ["/?home=1", "/people", "/account", "/references"])
def test_appearance_switch_survives_reload(browser_page, path):
    page, problems = browser_page
    _open(page, problems, path)
    toggle = page.locator('[data-theme-toggle], [data-static-theme-toggle]').first
    assert toggle.count() == 1
    original = page.locator('html').get_attribute('class') or ''
    was_dark = 'dark' in original.split()
    toggle.click()
    page.wait_for_function("expected => document.documentElement.classList.contains('dark') === expected", arg=not was_dark)
    for content in page.frames[1:]:
        content.wait_for_function("expected => document.documentElement.classList.contains('dark') === expected", arg=not was_dark)
    page.reload()
    page.wait_for_function("!document.documentElement.classList.contains('app-loading')")
    assert ('dark' in (page.locator('html').get_attribute('class') or '').split()) == (not was_dark)
    # The selected appearance also follows navigation to another page.
    page.goto(f"{BASE}/references")
    assert ('dark' in (page.locator('html').get_attribute('class') or '').split()) == (not was_dark)
    page.locator('[data-theme-toggle], [data-static-theme-toggle]').first.click()
    page.wait_for_function("expected => document.documentElement.classList.contains('dark') === expected", arg=was_dark)
    _check_common(page, problems, path + ' appearance')


@pytest.mark.parametrize("path", ["/?home=1", "/people", "/account", "/references"])
def test_mobile_pages_fit_the_viewport(browser_page, path):
    page, problems = browser_page
    page.set_viewport_size({"width": 390, "height": 844})
    try:
        _open(page, problems, path)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        switch = page.locator('[data-theme-toggle], [data-static-theme-toggle]').first
        assert switch.is_visible()
        assert page.locator('[data-recordings-toggle]').is_visible()
        for content in page.frames[1:]:
            assert content.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        bounds = switch.bounding_box()
        assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 390
        _check_common(page, problems, path + ' mobile')
    finally:
        page.set_viewport_size({"width": 1400, "height": 900})


def test_mobile_recording_keeps_title_and_player(browser_page):
    page, problems = browser_page
    listing = page.request.get(f"{BASE}/api/recordings?per_page=1")
    items = listing.json().get("recordings", []) if listing.ok else []

    if not items:
        pytest.skip('No recordings; the empty library is covered by test_main_view')
    page.set_viewport_size({"width": 390, "height": 844})
    try:
        _open(page, problems, f"/recordings/{items[0]['id']}")
        assert page.locator('.lv-mobile-title h1').inner_text().strip() == items[0]['title']
        assert page.locator('[data-mobile-player]').is_visible()
        assert page.locator('[data-mobile-bottom-nav]').is_visible()
        assert page.locator('[data-mobile-bottom-nav]').inner_text().split() == ['錄音庫', '逐字稿', '人物辨識', '錄音詳情']
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        _check_common(page, problems, 'recording mobile')
    finally:
        page.set_viewport_size({"width": 1400, "height": 900})


def test_login_page_and_standalone_appearance(browser_page):
    page, _ = browser_page
    context = page.context.browser.new_context(viewport={"width": 390, "height": 844})
    anonymous = context.new_page()
    problems = PageProblems(anonymous)
    try:
        _open(anonymous, problems, '/login')
        assert anonymous.locator('input[name=email]').is_visible()
        assert anonymous.locator('input[name=password]').is_visible()
        switch = anonymous.locator('[data-static-theme-toggle]')
        assert switch.is_visible()
        was_dark = 'dark' in (anonymous.locator('html').get_attribute('class') or '').split()
        switch.click()
        anonymous.reload()
        anonymous.wait_for_function("!document.documentElement.classList.contains('app-loading')")
        assert ('dark' in (anonymous.locator('html').get_attribute('class') or '').split()) == (not was_dark)
        assert anonymous.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        _check_common(anonymous, problems, 'login appearance and mobile')
    finally:
        context.close()


def test_recording_rename_seek_play_and_download(browser_page):
    if os.environ.get('SPEAKR_SMOKE_ALLOW_MUTATIONS') != '1':
        pytest.skip('Enable only against isolated synthetic fixtures: SPEAKR_SMOKE_ALLOW_MUTATIONS=1')
    page, problems = browser_page
    listing = page.request.get(f"{BASE}/api/recordings?per_page=100").json()['recordings']
    synthetic = [r for r in listing if r.get('title', '').startswith('UI smoke fixture ') and r.get('original_filename') == 'ui-smoke.wav']
    assert synthetic, 'Refusing to modify non-fixture recordings; run seed_browser_recordings.py in an isolated test instance'
    recording_id = synthetic[0]['id']
    _open(page, problems, f'/recordings/{recording_id}')
    speaker = page.locator('.lv-transcript-scroll .speaker-tablet').first
    original = speaker.inner_text().strip()
    replacement = original + '（介面測試）'
    speaker.dblclick()
    editor = page.locator('.speaker-inline-rename:visible')
    editor.fill(replacement)
    with page.expect_response(lambda r: r.url.endswith('/update_speakers') and r.request.method == 'POST') as saved:
        editor.press('Enter')
    assert saved.value.status == 200
    assert any(s.get('speaker') == replacement for s in json.loads(saved.value.json()['recording']['transcription']))
    renamed = page.locator('.lv-transcript-scroll .speaker-tablet').filter(has_text=replacement).first
    renamed.wait_for(state='visible')
    renamed.dblclick()
    editor = page.locator('.speaker-inline-rename:visible')
    editor.fill(original)
    with page.expect_response(lambda r: r.url.endswith('/update_speakers') and r.request.method == 'POST') as restored:
        editor.press('Enter')
    assert restored.value.status == 200
    page.locator('.speaker-inline-rename').wait_for(state='hidden')
    segment = page.locator('.lv-transcript-scroll .speaker-segment').nth(1)
    segment.dblclick()
    page.wait_for_function("() => { const a = document.getElementById('mainPlayerMedia'); return a && !a.paused && a.currentTime >= 8; }")
    page.locator('.lv-player .player-play-button').click()
    assert page.locator('#mainPlayerMedia').evaluate('(audio) => audio.paused')
    with page.expect_download() as exported:
        page.locator('.tabs-bar__actions button[title*="下載"]').click()
    download = exported.value
    assert download.suggested_filename.endswith('.txt')
    with open(download.path(), encoding='utf-8-sig') as stream:
        text = stream.read()
    assert '今天先確認一下產品的使用流程。' in text
    _check_common(page, problems, 'recording rename, playback, and TXT export')


def test_preferences_save_once_and_persist(browser_page):
    if os.environ.get('SPEAKR_SMOKE_ALLOW_MUTATIONS') != '1':
        pytest.skip('Enable only against an isolated test account')
    page, problems = browser_page
    _open(page, problems, '/account')
    checkbox = page.frame_locator('.lv-workspace-frame').locator('#prefEditorAutosave')
    original = checkbox.is_checked()
    submissions = []

    def track(request):
        if request.method == 'POST' and request.url == f'{BASE}/account':
            submissions.append(request)

    page.on('request', track)
    try:
        for desired in (not original, original):
            submissions.clear()
            checkbox.set_checked(desired)
            with page.expect_response(lambda r: r.url == f'{BASE}/account' and r.request.method == 'POST') as saved:
                page.frame_locator('.lv-workspace-frame').locator('button[form="preferencesForm"]').click()
            assert saved.value.status == 200
            page.wait_for_timeout(250)
            assert len(submissions) == 1, f'One save sent {len(submissions)} requests'
            page.reload()
            page.wait_for_function("!document.documentElement.classList.contains('app-loading')")
            assert checkbox.is_checked() == desired
        _check_common(page, problems, 'preferences save and persistence')
    finally:
        page.remove_listener('request', track)


def test_workspace_navigation_keeps_header_and_document(browser_page):
    page, problems = browser_page
    _open(page, problems, '/?home=1')
    navigations = []
    def track(request):
        if request.is_navigation_request() and request.frame == page.main_frame:
            navigations.append(request.url)
    page.on('request', track)
    try:
        assert page.locator('[data-workspace-link]').count() == 3
        assert page.locator('[data-recording-capture]:visible').count() == 0
        assert page.locator('.lv-hero').bounding_box()['y'] < 200
        assert page.locator('.lv-start-card:visible').count() == 1
        assert '帶入你的錄音' in page.locator('.lv-start-card').inner_text()
        assert '聲音對上名字' not in page.inner_text('body')
        for target in ('people', 'account', 'recordings'):
            page.locator(f'[data-workspace-link="{target}"]').click()
            page.wait_for_url(f'{BASE}/?home=1' if target == 'recordings' else f'{BASE}/{target}')
            assert page.locator('[data-recordings-toggle]').is_visible()
            if target != 'recordings':
                frame = page.frame_locator('.lv-workspace-frame')
                frame.locator('html.lv-embedded').wait_for()
                assert frame.locator('body > header:visible, body > #global-header:visible').count() == 0
                assert page.locator('.lv-workspace-frame').bounding_box()['height'] > 500
        assert not navigations, f'Top-level document was reloaded: {navigations}'
        page.go_back()
        page.wait_for_url(f'{BASE}/account')
        assert page.frame_locator('.lv-workspace-frame').locator('#prefEditorAutosave').is_visible()
        page.go_forward()
        page.wait_for_url(f'{BASE}/?home=1')
        page.locator('.lv-workspace-frame').wait_for(state='detached')
        _check_common(page, problems, 'seamless workspace navigation')
    finally:
        page.remove_listener('request', track)


def test_library_opens_recording_from_settings(browser_page):
    page, problems = browser_page
    _open(page, problems, '/account')
    toggle = page.locator('[data-recordings-toggle]')
    # Toggle twice: both collapse and restore must remain available on settings.
    toggle.click()
    toggle.click()
    listing = page.request.get(f'{BASE}/api/recordings?per_page=1').json()['recordings']
    assert listing
    page.locator('aside').get_by_text(listing[0]['title'], exact=True).first.click()
    page.wait_for_url(f"{BASE}/recordings/{listing[0]['id']}")
    assert page.locator('.lv-workspace-frame').count() == 0
    assert page.locator('[data-recordings-toggle]').is_visible()
    _check_common(page, problems, 'select recording from settings')


def test_about_links_and_credits(browser_page):
    page, problems = browser_page
    _open(page, problems, '/account')
    frame = page.frame_locator('.lv-workspace-frame')
    assert frame.locator('a[href="https://github.com/SamLiaoP/locol_vocol"]').count() >= 1
    assert frame.locator('a[href="https://github.com/SamLiaoP/locol_vocol#readme"]').count() >= 1
    assert frame.locator('a[href*="hub.docker.com"]').count() == 0
    page.locator('[data-user-menu-toggle]').click()
    page.locator('[data-user-menu-dropdown] a[href="/references"]').click()
    page.wait_for_url(f'{BASE}/references')
    assert page.locator('[data-recordings-toggle]').is_visible()
    assert page.frame_locator('.lv-workspace-frame').get_by_alt_text('Speakr 開源專案 Logo').is_visible()
    _check_common(page, problems, 'project links and credits')
