from __future__ import annotations

from html import escape
from math import isfinite
from urllib.parse import quote

from bridge.config import BridgeConfig, Settings
from bridge.store import DOWNLOADABLE_QUEUE_STATUSES, TERMINAL_QUEUE_STATUSES

BASE_CSS = """
    :root {
      color-scheme: dark;
      --bg: #111315;
      --panel: #191d20;
      --line: #2b3137;
      --text: #eef2f5;
      --muted: #9aa6b2;
      --accent: #80d0c7;
      --warn: #f1c40f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
    }
    .muted { color: var(--muted); }
    main { margin: 0 auto; padding: 24px 28px 40px; }
    table { width: 100%; border-collapse: collapse; }
    th, td {
      text-align: left;
      vertical-align: top;
      padding: 9px 8px;
      border-bottom: 1px solid var(--line);
    }
    th { color: var(--muted); font-size: 12px; font-weight: 600; }
"""


def _page_shell(title: str, extra_css: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
{BASE_CSS}
{extra_css}
  </style>
</head>
<body>
{body}
</body>
</html>"""


def dashboard_html(
    settings: Settings,
    config: BridgeConfig,
    queue_items: list[dict] | None = None,
    validation_issues: list[dict] | None = None,
) -> str:
    queue_items = queue_items or []
    validation_issues = validation_issues or []
    profiles = "\n".join(
        f"""
        <tr>
          <td>{escape(str(profile.id))}</td>
          <td>{escape(profile.name)}</td>
          <td>{escape(", ".join(profile.media_types))}</td>
          <td><span class="pill">{escape(profile.mode)}</span></td>
          <td>{escape(profile.mediamanager_library)}</td>
          <td>{escape(profile.mediamanager_ruleset)}</td>
          <td>{escape(profile.score_set)}</td>
          <td>{escape(", ".join(profile.trash_custom_format_group_ids))}</td>
          <td>{optional_text_html(profile.profilarr_profile_id)}</td>
        </tr>
        """
        for profile in config.profiles
    )
    roots = "\n".join(
        (
            f"<tr><td>{escape(str(root.id))}</td><td>{escape(root.path)}</td>"
            f"<td>{escape(str(root.free_space))}</td></tr>"
        )
        for root in config.root_folders
    )
    tags = "\n".join(
        f"<span class=\"tag\">#{escape(str(tag.id))} {escape(tag.label)}</span>"
        for tag in config.tags
    )
    queue = "\n".join(
        f"""
        <tr>
          <td>{escape(str(item.get("title") or ""))}</td>
          <td>{escape(str(item.get("media_type") or ""))}</td>
          <td><span class="pill">{escape(str(item.get("status") or ""))}</span></td>
          <td>{escape(queue_profile_label(config, item))}</td>
          <td>{escape(str(item.get("mediamanager_id") or ""))}</td>
          <td>{candidate_table_html(item)}</td>
          <td>{escape(event_message_text(item.get("last_event")))}</td>
          <td>{escape(event_message_text(item.get("last_error")))}</td>
          <td>{queue_controls_html(item)}</td>
        </tr>
        """
        for item in queue_items
    ) or """<tr><td colspan="9" class="muted">No queued requests</td></tr>"""
    issues = "\n".join(
        f"""
        <tr>
          <td><span class="pill">{escape(str(issue.get("severity") or ""))}</span></td>
          <td>{escape(str(issue.get("code") or ""))}</td>
          <td>{escape(str(issue.get("message") or ""))}</td>
        </tr>
        """
        for issue in validation_issues
    ) or """<tr><td colspan="3" class="muted">No local validation warnings</td></tr>"""
    mm_url = escape(str(settings.mediamanager_base_url))

    extra_css = """
    header {
      border-bottom: 1px solid var(--line);
      padding: 18px 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    main { max-width: 1180px; }
    h1 { font-size: 22px; margin: 0; letter-spacing: 0; }
    h2 { font-size: 15px; margin: 0 0 12px; letter-spacing: 0; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
    .panel {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 16px;
    }
    tr:last-child td { border-bottom: 0; }
    code {
      background: #0d0f10;
      border: 1px solid var(--line);
      padding: 2px 6px;
      border-radius: 5px;
      color: var(--accent);
    }
    .pill, .tag {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 9px;
      color: var(--accent);
      background: #101818;
      margin: 0 6px 6px 0;
    }
    .status { color: var(--accent); font-weight: 700; }
    .span-2 { grid-column: 1 / -1; }
    .actions { display: flex; flex-wrap: wrap; gap: 6px; min-width: 160px; }
    .candidate-list { display: grid; gap: 8px; min-width: 280px; }
    .candidate {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }
    .candidate.selected { border-left: 2px solid var(--accent); padding-left: 6px; }
    .candidate-title { font-weight: 700; overflow-wrap: anywhere; }
    .candidate-meta { color: var(--muted); font-size: 12px; }
    button, .button {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 5px 8px;
      background: #0d0f10;
      color: var(--text);
      cursor: pointer;
      text-decoration: none;
      font: inherit;
    }
    button:hover, .button:hover { border-color: var(--accent); }
    button:focus-visible, .button:focus-visible {
      border-color: var(--accent);
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    @media (max-width: 760px) {
      header { align-items: flex-start; flex-direction: column; }
      main { padding: 18px 14px 32px; }
      .grid { grid-template-columns: 1fr; }
      .span-2 { grid-column: auto; }
      table { display: block; overflow-x: auto; }
    }
"""
    body = f"""
  <header>
    <div>
      <h1>Screenarr</h1>
      <div class="muted">OnScreen to MediaManager bridge</div>
    </div>
    <div class="status">Running</div>
  </header>
  <main>
    <section class="grid">
      <div class="panel">
        <h2>MediaManager</h2>
        <p class="muted">Base URL</p>
        <p><code>{mm_url}</code></p>
      </div>
      <div class="panel">
        <h2>OnScreen Setup</h2>
        <p class="muted">Use this URL for Radarr and Sonarr services inside Docker.</p>
        <p><code>http://screenarr:7879</code></p>
      </div>
      <div class="panel span-2">
        <h2>Profiles</h2>
        <table>
          <thead>
            <tr>
              <th>ID</th><th>Name</th><th>Types</th><th>Mode</th>
              <th>Library</th><th>Ruleset</th><th>Score Set</th>
              <th>TRaSH Groups</th><th>Profilarr ID</th>
            </tr>
          </thead>
          <tbody>{profiles}</tbody>
        </table>
      </div>
      <div class="panel span-2">
        <h2>Queue</h2>
        <table>
          <thead>
            <tr>
              <th>Title</th><th>Type</th><th>Status</th><th>Profile</th>
              <th>MediaManager ID</th><th>Candidates</th><th>Latest Event</th>
              <th>Latest Error</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>{queue}</tbody>
        </table>
      </div>
      <div class="panel span-2">
        <h2>Validation</h2>
        <table>
          <thead><tr><th>Severity</th><th>Code</th><th>Message</th></tr></thead>
          <tbody>{issues}</tbody>
        </table>
      </div>
      <div class="panel">
        <h2>Root Folders</h2>
        <table>
          <thead><tr><th>ID</th><th>Path</th><th>Free Space</th></tr></thead>
          <tbody>{roots}</tbody>
        </table>
      </div>
      <div class="panel">
        <h2>Tags</h2>
        <div>{tags}</div>
      </div>
    </section>
  </main>
"""
    return _page_shell("Screenarr", extra_css, body)


def events_html(queue_id: str, events: list[dict]) -> str:
    rows = "\n".join(
        f"""
        <tr>
          <td>{escape(str(event.get("created_at") or ""))}</td>
          <td>{escape(str(event.get("event_type") or ""))}</td>
          <td>{escape(str(event.get("message") or ""))}</td>
        </tr>
        """
        for event in events
    ) or """<tr><td colspan="3" class="muted">No events recorded</td></tr>"""
    extra_css = """
    main { max-width: 1000px; }
    h1 { font-size: 22px; margin: 0 0 6px; letter-spacing: 0; }
    a { color: var(--accent); }
    table { margin-top: 18px; }
"""
    body = f"""
  <main>
    <h1>Queue Events</h1>
    <div class="muted">{escape(queue_id)}</div>
    <p><a href="/dashboard">Back to dashboard</a></p>
    <table>
      <thead><tr><th>Time</th><th>Type</th><th>Message</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </main>
"""
    return _page_shell("Screenarr Events", extra_css, body)


def queue_profile_label(config: BridgeConfig, item: dict) -> str:
    profile_id = item.get("profile_id")
    media_type = item.get("media_type")
    fallback = str(profile_id) if profile_id is not None else ""
    if profile_id is None or media_type is None:
        return fallback
    try:
        profile = config.profile_for(int(profile_id), str(media_type))
    except (TypeError, ValueError):
        return fallback
    return profile.name


def candidate_table_html(item: dict) -> str:
    candidates = item.get("candidates") or []
    if not candidates:
        return '<span class="muted">None</span>'
    selected_id = selected_candidate_id(item)
    queue_id = path_segment(item.get("id"))
    downloadable = item.get("status") in DOWNLOADABLE_QUEUE_STATUSES
    candidate_rows = "".join(
        candidate_row_html(
            candidate,
            selected_id=selected_id,
            queue_id=queue_id,
            downloadable=downloadable,
        )
        for candidate in candidates
    )
    return f'<div class="candidate-list">{candidate_rows}</div>'


def candidate_row_html(
    candidate: dict,
    *,
    selected_id: str,
    queue_id: str,
    downloadable: bool,
) -> str:
    candidate_id = raw_text(candidate.get("id"))
    selected = " selected" if selected_id and candidate_id == selected_id else ""
    download_form = ""
    if candidate_id and downloadable:
        safe_candidate_id = path_segment(candidate_id)
        download_form = post_form_html(
            f"/dashboard/queue/{queue_id}/download/{safe_candidate_id}",
            "Download this release",
        )
    return f"""
        <div class="candidate{selected}">
          <div>
            <div class="candidate-title">{escape(str(candidate.get("title") or ""))}</div>
            <div class="candidate-meta">
              score {escape(str(candidate.get("score") or 0))}
              · seeders {escape(str(candidate.get("seeders") or 0))}
              · {escape(format_size(candidate.get("size") or 0))}
            </div>
          </div>
          {download_form}
        </div>
        """


def queue_controls_html(item: dict) -> str:
    queue_id = path_segment(item.get("id"))
    controls = []
    if item.get("status") not in TERMINAL_QUEUE_STATUSES:
        controls.extend(
            [
                post_form_html(
                    f"/dashboard/queue/{queue_id}/refresh-candidates",
                    "Refresh",
                ),
                post_form_html(f"/dashboard/queue/{queue_id}/reconcile", "Reconcile"),
            ]
        )
    events_href = escape(f"/dashboard/queue/{queue_id}/events", quote=True)
    controls.append(f"""<a class="button" href="{events_href}">Events</a>""")
    return f'<div class="actions">{"".join(controls)}</div>'


def post_form_html(action: str, label: str) -> str:
    return f"""
        <form method="post" action="{escape(action, quote=True)}">
          <button type="submit">{escape(label)}</button>
        </form>
        """


def selected_candidate_id(item: dict) -> str:
    event = item.get("last_event")
    if not isinstance(event, dict):
        return ""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""
    return raw_text(payload.get("candidate_id"))


def path_segment(value: object) -> str:
    return quote(raw_text(value), safe="")


def raw_text(value: object) -> str:
    return "" if value is None else str(value)


def optional_text_html(value: object) -> str:
    text = raw_text(value)
    if not text:
        return '<span class="muted">None</span>'
    return escape(text)


def event_message_text(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    created_at = str(event.get("created_at") or "")
    message = str(event.get("message") or "")
    return f"{created_at} {message}".strip()


def format_size(value: object) -> str:
    try:
        size = float(value)
    except (OverflowError, TypeError, ValueError):
        size = 0.0
    if not isfinite(size):
        size = 0.0
    size = max(size, 0.0)
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GiB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{int(size)} B"


def dashboard_login_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Screenarr Login</title>
  <style>
    :root { color-scheme: dark; --bg: #111315; --line: #2b3137; --text: #eef2f5; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    form { width: min(360px, calc(100vw - 32px)); display: grid; gap: 12px; }
    label { font-weight: 700; }
    input, button {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      font: inherit;
    }
    button { cursor: pointer; font-weight: 700; }
  </style>
</head>
<body>
  <form method="post" action="/dashboard/login">
    <label for="api_key">Screenarr API key</label>
    <input id="api_key" name="api_key" type="password" autocomplete="current-password" required>
    <button type="submit">Open dashboard</button>
  </form>
</body>
</html>"""
