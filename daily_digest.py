#!/usr/bin/env python3
"""Daily governance digest email.

Builds a short HTML + plain-text summary of what changed in the last
DIGEST_HOURS (default 24) across the same public sources the dashboard
uses, then sends it over SMTP.

Recipients are never stored in this repo. They come from the
DIGEST_RECIPIENTS secret (comma- or newline-separated) and are sent as
Bcc, so no recipient sees another's address and nothing appears in logs.

Environment (all from Actions secrets / variables):
  DIGEST_RECIPIENTS   addresses, comma/newline separated (secret)
  SMTP_HOST           e.g. smtp.gmail.com
  SMTP_PORT           465 (SSL) or 587 (STARTTLS); default 465
  SMTP_USERNAME       login for the sending account (secret)
  SMTP_PASSWORD       app password / API key (secret)
  DIGEST_FROM         From: address; defaults to SMTP_USERNAME
  DIGEST_HOURS        look-back window; default 24
  DIGEST_SKIP_EMPTY   "1" to skip sending when nothing changed

Without SMTP settings it writes digest-preview.html and exits, which is
how to preview the template locally.
"""

import html as htmllib
import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import fip_utils as u
from fip_utils import html_escape as esc

SITE = 'https://thatguyllc.github.io/fips-dashboard/'
BATCH = 90  # stay under per-message recipient caps (Gmail SMTP: 100)

# Inline styles: email clients ignore <style> blocks unevenly.
C = {
    'ink': '#0B1B2E', 'muted': '#5B6B80', 'line': '#E3E8EF', 'bg': '#F4F7FB', 'blue': '#0090FF',
    'amber_bg': '#FFF0D1', 'amber_fg': '#8A5300', 'green_bg': '#DDF5E8', 'green_fg': '#0B6B3A',
    'red_bg': '#FDE2E1', 'red_fg': '#A1251B', 'grey_bg': '#E6EBF5', 'grey_fg': '#34476B',
}
PILL = {'ready': ('green_bg', 'green_fg'), 'risk': ('amber_bg', 'amber_fg'),
        'deferred-in': ('red_bg', 'red_fg')}
READY = {'ready': 'Ready', 'risk': 'Candidate', 'deferred-in': 'Carry-over', 'deferred-out': 'Next upgrade',
         'none': 'Not upgrade-bound', 'unknown': 'In planning'}


def pill(text, kind='grey'):
    bg, fg = PILL[kind] if kind in PILL else (f'{kind}_bg', f'{kind}_fg')
    return (f'<span style="display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;border-radius:5px;'
            f'background:{C.get(bg, C["grey_bg"])};color:{C.get(fg, C["grey_fg"])};white-space:nowrap">{esc(text)}</span>')


def a(url, text):
    return f'<a href="{esc(url)}" style="color:{C["blue"]};text-decoration:none;font-weight:600">{esc(text)}</a>'


def fip_a(num):
    return a(u.fip_url(num), f'FIP-{num}')


def section(title, items, empty):
    rows = ''.join(f'<tr><td style="padding:8px 0;border-top:1px solid {C["line"]};font-size:14px;line-height:1.45">{i}</td></tr>'
                   for i in items)
    body = (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{rows}</table>' if items
            else f'<p style="margin:6px 0 0;color:{C["muted"]};font-size:14px">{esc(empty)}</p>')
    return (f'<tr><td style="padding:18px 24px 6px"><h2 style="margin:0 0 4px;font-size:16px;color:{C["ink"]}">{esc(title)}</h2>'
            f'{body}</td></tr>')


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def collect(hours):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    d = {'now': now, 'since': since, 'hours': hours}

    repo = u.get_fips_repo()
    fips = {f['number']: f for f in u.parse_fip_table(u.read_readme(repo))}
    history, events = u.build_status_history(repo)
    files = u.load_fip_files(repo)
    open_lc, _ = u.last_call_windows(history, files, fips)
    d['fips'] = fips
    d['status_changes'] = [e for e in events if e['date'] >= since and e['from'] != e['to']]
    d['last_call'] = open_lc

    raw = u.fetch_open_prs()
    prs = u.process_prs(raw, with_reviews=False) if raw is not None else []
    d['prs_loaded'] = raw is not None
    for p in prs:
        p['created'] = datetime.fromisoformat(p['created_at'].replace('Z', '+00:00'))
        p['updated'] = datetime.fromisoformat(p['updated_at'].replace('Z', '+00:00'))
    d['prs_new'] = [p for p in prs if p['created'] >= since]
    d['prs_active'] = [p for p in prs if p['updated'] >= since and p['created'] < since]

    discussions = []
    for owner, name in (('filecoin-project', 'FIPs'), ('filecoin-project', 'core-devs')):
        discussions += u.fetch_discussions(owner, name, 15) or []
    d['discussions_loaded'] = bool(discussions)
    d['discussions'] = sorted((x for x in discussions if x['updated'] >= since),
                              key=lambda x: x['updated'], reverse=True)

    d['plan'], d['flags'], d['meetings'], d['calls'], d['agenda'] = None, {}, [], [], []
    try:
        import core_devs as cd
        cd_repo = cd.get_core_devs_repo()
        meetings = cd.load_meetings(cd_repo)
        upgrades = cd.load_upgrades(cd_repo)
        d['meetings'] = [m for m in meetings if m.get('date') and m['date'] >= since - timedelta(days=1)]
        d['agenda'] = cd.fetch_open_agenda() or []
        import scope_planning as sp
        plans = sp.active(sp.fetch_planning(), upgrades)
        if plans:
            d['plan'] = plans[0]
            titles = {n: f['title'] for n, f in fips.items()}
            d['flags'] = sp.fip_flags(plans, titles, files, prs)
    except Exception as e:
        print(f'Warning: core-devs / planning data unavailable: {e}')
    try:
        import implementers as impl
        calls, _ = impl.load_calls()
        d['calls'] = [c for c in calls if c.get('date') and c['date'] >= since - timedelta(days=1)]
    except Exception as e:
        print(f'Warning: implementers calls unavailable: {e}')
    return d


def has_news(d):
    return any([d['status_changes'], d['prs_new'], d['prs_active'], d['discussions'], d['meetings'], d['calls'],
                any((w['end'] - d['now']).days <= 3 for w in d['last_call'])])


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(d):
    now, hours = d['now'], d['hours']
    window = ('the last 24 hours' if hours == 24 else
              f'the last {hours // 24} days' if hours % 24 == 0 else f'the last {hours} hours')
    fips = d['fips']

    # Status changes
    changes = []
    for e in sorted(d['status_changes'], key=lambda e: e['date'], reverse=True):
        frm = esc(e['from']) if e['from'] else 'new'
        to = esc(e['to']) if e['to'] else 'removed'
        changes.append(f'{fip_a(e["number"])} {esc(e["title"])}<br>'
                       f'<span style="color:{C["muted"]}">{frm} → <strong style="color:{C["ink"]}">{to}</strong></span>')

    # Last Call
    lc = []
    for w in sorted(d['last_call'], key=lambda w: w['end']):
        left = (w['end'] - now).days
        when = f'{left} days left' if left > 1 else ('1 day left' if left == 1 else 'ends today' if left == 0 else 'overdue')
        disc = f' · {a(w["discussions"][0], "comment")}' if w['discussions'] else ''
        lc.append(f'{fip_a(w["number"])} {esc(w["title"])} '
                  f'{pill(when, "red" if left <= 3 else "amber")}<br>'
                  f'<span style="color:{C["muted"]}">Review ends {w["end"]:%b %-d}{" (est.)" if w["estimated_end"] else ""}</span>{disc}')

    # Active planning
    plan_items = []
    if d['plan']:
        p = d['plan']
        for r in p['scope']:
            name = esc(r['name'])
            if r['fip']:
                name = re.sub(r'FIP-?\d+', fip_a(r['fip']), name, count=1)
            elif r.get('link'):
                name = a(r['link'], r['name'])
            plan_items.append(f'{pill(READY[r["readiness"]], r["readiness"] if r["readiness"] in PILL else "grey")} {name}'
                              + (f'<br><span style="color:{C["muted"]}">{esc(r["status"][:140])}</span>' if r['status'] else ''))

    # PRs
    def pr_line(p, tag):
        subj = ', '.join(fip_a(n) for n in p['fip_numbers'] if n != 'General')
        return (f'{pill(tag, "green" if tag == "New" else "grey")} {a(p["url"], "#" + str(p["number"]))} {esc(p["title"])}'
                f'<br><span style="color:{C["muted"]}">@{esc(p["author"])}{" · " + subj if subj else ""}</span>')
    prs = [pr_line(p, 'New') for p in d['prs_new']] + [pr_line(p, 'Updated') for p in d['prs_active']]

    # Discussions
    disc = []
    for x in d['discussions']:
        tag = 'New' if x['created'] >= d['since'] else 'Active'
        disc.append(f'{pill(tag, "green" if tag == "New" else "grey")} {a(x["url"], x["title"])}'
                    f'<br><span style="color:{C["muted"]}">{esc(x["repo"].split("/")[1])} #{x["number"]} · '
                    f'{x["comments"]} comments · @{esc(x["author"])}</span>')

    # Calls
    calls = []
    for m in d['meetings']:
        link = m.get('notes_url') or m.get('recording_url') or m.get('agenda_url')
        title = f'Core Devs #{m["number"]}'
        calls.append(f'{pill("Core Devs", "grey")} {a(link, title) if link else esc(title)} · {m["date"]:%b %-d}'
                     + (f'<br><span style="color:{C["muted"]}">FIPs: {", ".join("FIP-" + f for f in m["fips"][:8])}</span>' if m.get('fips') else ''))
    for c in d['calls']:
        link = c.get('notes_url') or c.get('page_url')
        calls.append(f'{pill("Implementers", "grey")} {a(link, c["title"]) if link else esc(c["title"])} · {c["date"]:%b %-d}'
                     + (f'<br><span style="color:{C["muted"]}">{esc(c["summary"][:200])}</span>' if c.get('summary') else ''))
    for g in d['agenda'][:2]:
        calls.append(f'{pill("Upcoming", "amber")} {a(g["url"], g["title"])}')

    # Summary line
    plan_n = len(d['plan']['scope']) if d['plan'] else 0
    stats = [
        (len(d['status_changes']), 'status changes'),
        (len(d['last_call']), 'in Last Call'),
        (len(d['prs_new']) + len(d['prs_active']), 'PRs with activity'),
        (len(d['discussions']), 'active discussions'),
    ]
    stat_cells = ''.join(
        f'<td align="center" style="padding:10px 4px;background:{C["bg"]};border-radius:8px">'
        f'<div style="font-size:22px;font-weight:800;color:{C["ink"]}">{n}</div>'
        f'<div style="font-size:11px;color:{C["muted"]};text-transform:uppercase;letter-spacing:.04em">{esc(t)}</div></td>'
        f'<td width="8"></td>' for n, t in stats)

    sections = [
        section('Status changes', changes, f'No FIP changed status in {window}.'),
        section('Last Call', lc, 'No FIPs are in Last Call.'),
    ]
    if d['plan']:
        sections.append(section(f'Active upgrade planning · NV{d["plan"]["nv"]} ({plan_n} items)', plan_items,
                                'No scope table yet.')
                        .replace('</h2>', f'</h2><p style="margin:0 0 4px;font-size:12px;color:{C["muted"]}">'
                                 f'From {a(d["plan"]["url"], "the planning discussion")}. Candidates are not commitments.</p>', 1))
    sections += [
        section('Pull requests', prs, f'No FIP PR activity in {window}.' if d['prs_loaded'] else 'PRs could not be loaded on this run.'),
        section('Discussions', disc, f'No discussion activity in {window}.' if d['discussions_loaded'] else 'Discussions could not be loaded on this run.'),
        section('Calls', calls, f'No Core Devs or Implementers calls in {window}.'),
    ]

    date_label = f'{now:%A, %B %-d, %Y}'
    html = f'''<!doctype html><html><body style="margin:0;padding:0;background:{C["bg"]};font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{C["ink"]}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{C["bg"]}"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:100%;background:#fff;border:1px solid {C["line"]};border-radius:12px;overflow:hidden">
<tr><td style="background:{C["ink"]};padding:20px 24px">
<div style="font-size:11px;font-weight:700;letter-spacing:.12em;color:#7FC4FF;text-transform:uppercase">Filecoin governance · daily digest</div>
<div style="font-size:22px;font-weight:800;color:#fff;margin-top:4px">FIP updates for {esc(date_label)}</div>
<div style="font-size:13px;color:#B9C6D6;margin-top:4px">Changes in {esc(window)}, from the FIPs and core-devs repos.</div>
</td></tr>
<tr><td style="padding:18px 24px 0"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>{stat_cells}</tr></table></td></tr>
{"".join(sections)}
<tr><td style="padding:18px 24px 22px;border-top:1px solid {C["line"]};font-size:12px;color:{C["muted"]};line-height:1.5">
{a(SITE, "Open the dashboard")} · {a(SITE + "network-upgrades.html", "Upgrades")} · {a(SITE + "fips-dashboard-static.html", "All FIPs")}<br>
Read-only community digest generated from public GitHub data. It doesn't change FIP status.
You're receiving this because you were added to the digest list; reply to the sender to be removed.
</td></tr></table></td></tr></table></body></html>'''

    # Plain-text alternative
    def strip(s):
        return htmllib.unescape(re.sub(r'\s+', ' ', re.sub(r'<br>', ' — ', re.sub(r'<(?!br)[^>]+>', '', s))).strip())
    text = [f'Filecoin FIP digest — {date_label}', f'Changes in {window}.', '']
    for title, items in (('STATUS CHANGES', changes), ('LAST CALL', lc),
                         (f'ACTIVE UPGRADE PLANNING (NV{d["plan"]["nv"]})' if d['plan'] else '', plan_items),
                         ('PULL REQUESTS', prs), ('DISCUSSIONS', disc), ('CALLS', calls)):
        if not title:
            continue
        text.append(title)
        text += [f'- {strip(i)}' for i in items] or ['- none']
        text.append('')
    text.append(f'Dashboard: {SITE}')
    subject = f'FIP digest {now:%b %-d}: ' + ', '.join(f'{n} {t}' for n, t in stats if n) if has_news(d) else f'FIP digest {now:%b %-d}: no changes'
    return subject, html, '\n'.join(text)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def recipients():
    raw = os.environ.get('DIGEST_RECIPIENTS', '')
    return [r.strip() for r in re.split(r'[,\n;]', raw) if '@' in r]


def send(subject, html, text):
    host, user, pw = os.environ.get('SMTP_HOST'), os.environ.get('SMTP_USERNAME'), os.environ.get('SMTP_PASSWORD')
    port = int(os.environ.get('SMTP_PORT') or 465)
    sender = os.environ.get('DIGEST_FROM') or user
    to = recipients()
    if not (host and user and pw and to):
        missing = [k for k, v in (('SMTP_HOST', host), ('SMTP_USERNAME', user), ('SMTP_PASSWORD', pw), ('DIGEST_RECIPIENTS', to)) if not v]
        print(f'Not sending: missing {", ".join(missing)}. Wrote digest-preview.html instead.')
        return False
    ctx = ssl.create_default_context()
    server = smtplib.SMTP_SSL(host, port, context=ctx) if port == 465 else smtplib.SMTP(host, port)
    with server:
        if port != 465:
            server.starttls(context=ctx)
        server.login(user, pw)
        for i in range(0, len(to), BATCH):
            msg = EmailMessage()
            msg['Subject'], msg['From'], msg['To'] = subject, sender, sender
            msg['Date'], msg['Message-ID'] = formatdate(localtime=False), make_msgid()
            msg.set_content(text)
            msg.add_alternative(html, subtype='html')
            # Bcc only: addresses go in the SMTP envelope, never in headers.
            server.send_message(msg, from_addr=sender, to_addrs=to[i:i + BATCH])
    print(f'Sent digest to {len(to)} recipient(s) in {(len(to) + BATCH - 1) // BATCH} message(s).')
    return True


def main():
    hours = int(os.environ.get('DIGEST_HOURS') or 24)
    d = collect(hours)
    subject, html, text = render(d)
    print(f'Subject: {subject}')
    with open('digest-preview.html', 'w', encoding='utf-8') as f:
        f.write(html)
    if os.environ.get('DIGEST_SKIP_EMPTY') == '1' and not has_news(d):
        print('Nothing changed; skipping send.')
        return 0
    send(subject, html, text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
