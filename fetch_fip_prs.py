#!/usr/bin/env python3
"""
Script to fetch open PRs related to FIPs from GitHub
"""

import re
from datetime import datetime
from fip_utils import html_escape, make_github_request

GITHUB_API_BASE = 'https://api.github.com/repos/filecoin-project/FIPs'


def fetch_open_prs():
    """Fetch all open pull requests with pagination."""
    all_prs = []
    page = 1

    while True:
        url = f"{GITHUB_API_BASE}/pulls?state=open&per_page=100&page={page}"
        try:
            prs = make_github_request(url)
            if not prs:
                break
            all_prs.extend(prs)
            if len(prs) < 100:
                break
            page += 1
        except Exception as e:
            print(f"Error fetching PRs (page {page}): {e}")
            break

    return all_prs


def extract_fip_from_title(title):
    """Extract the subject FIP number from a PR title only.

    Only looks at the title to avoid false associations from
    body references (e.g. 'builds on FIP-0019' is context, not subject).
    Skips placeholder patterns like FIPXXXX.
    """
    if not title:
        return []

    # Skip placeholder patterns (FIPXXXX, FIP-XXXX)
    if re.search(r'FIP[-\s]?[Xx]{2,}', title, re.IGNORECASE):
        return []

    fip_numbers = set()
    matches = re.findall(r'FIP[-\s]?(\d{1,4})', title, re.IGNORECASE)
    for match in matches:
        fip_numbers.add(match.zfill(4))
    return sorted(list(fip_numbers))


def extract_fip_from_files(pr_number):
    """Extract FIP numbers from the actual files changed in a PR.

    This is the ground truth - if a PR modifies FIPS/fip-0117.md,
    then it's about FIP-0117 regardless of what the title/body say.
    """
    try:
        files = make_github_request(
            f"{GITHUB_API_BASE}/pulls/{pr_number}/files"
        )
        fip_numbers = set()
        for f in files:
            m = re.search(r'FIPS/fip-(\d+)\.md', f.get('filename', ''), re.IGNORECASE)
            if m:
                fip_numbers.add(m.group(1).zfill(4))
        return sorted(list(fip_numbers))
    except Exception as e:
        print(f"Warning: Could not fetch files for PR #{pr_number}: {e}")
        return []


def categorize_pr(pr):
    """Categorize PR based on its content."""
    title = pr.get('title', '').lower()
    body = (pr.get('body') or '').lower()
    combined = f"{title} {body}"

    categories = []

    if 'new' in title or 'add' in title or 'create' in title:
        categories.append('New FIP')
    if 'update' in title or 'modify' in title or 'change' in title:
        categories.append('Update FIP')
    if 'status' in combined or 'final' in combined or 'draft' in combined:
        categories.append('Status Change')
    if 'supersede' in combined or 'replace' in combined:
        categories.append('Supersede')

    return categories if categories else ['General']


def process_prs(prs):
    """Process PRs and extract FIP information.

    Uses a priority system:
    1. FIP numbers from actual files changed (ground truth)
    2. FIP numbers from the PR title (subject FIP)
    3. If neither found, check if generally FIP-related
    Body text is NOT searched to avoid false associations.
    """
    fip_prs = []

    for pr in prs:
        title = pr.get('title', '')
        body = pr.get('body') or ''  # Guard against None
        number = pr.get('number')
        html_url = pr.get('html_url')
        user = pr.get('user', {}).get('login', 'Unknown')
        created_at = pr.get('created_at', '')
        updated_at = pr.get('updated_at', '')
        branch = pr.get('head', {}).get('ref', '')

        # Priority 1: Check actual files changed
        fip_numbers = extract_fip_from_files(number)

        # Priority 2: Fall back to title extraction
        if not fip_numbers:
            fip_numbers = extract_fip_from_title(title)

        # If still nothing, mark as General if FIP-related
        if not fip_numbers:
            if 'fip' in title.lower():
                fip_numbers = ['General']
            else:
                continue

        categories = categorize_pr(pr)

        fip_prs.append({
            'pr_number': number,
            'title': title,
            'body': body[:200] + '...' if len(body) > 200 else body,
            'url': html_url,
            'author': user,
            'created_at': created_at,
            'updated_at': updated_at,
            'branch': branch,
            'fip_numbers': fip_numbers,
            'categories': categories
        })

    return fip_prs


def generate_pr_html(fip_prs):
    """Generate HTML for PRs section with XSS-safe output."""
    if not fip_prs:
        return '<div class="no-prs">No open PRs found.</div>'

    # Group PRs by FIP number
    prs_by_fip = {}
    general_prs = []

    for pr in fip_prs:
        if pr['fip_numbers'] == ['General'] or not pr['fip_numbers']:
            general_prs.append(pr)
        else:
            for fip_num in pr['fip_numbers']:
                if fip_num not in prs_by_fip:
                    prs_by_fip[fip_num] = []
                prs_by_fip[fip_num].append(pr)

    html = '<div class="prs-container">'

    # PRs grouped by FIP
    if prs_by_fip:
        html += '<div class="prs-section"><h3>PRs by FIP</h3>'
        for fip_num in sorted(prs_by_fip.keys()):
            prs = prs_by_fip[fip_num]
            safe_fip = html_escape(fip_num)
            html += f'<div class="fip-pr-group">'
            html += f'<div class="fip-pr-header"><strong>FIP-{safe_fip}</strong> <span class="pr-count">({len(prs)} PR{"s" if len(prs) > 1 else ""})</span></div>'
            html += '<div class="pr-list">'
            for pr in prs:
                created_date = datetime.fromisoformat(pr['created_at'].replace('Z', '+00:00')).strftime('%Y-%m-%d')
                safe_title = html_escape(pr['title'])
                safe_author = html_escape(pr['author'])
                safe_url = html_escape(pr['url'])
                safe_branch = html_escape(pr['branch'])
                safe_categories = html_escape(', '.join(pr['categories']))
                safe_body = html_escape(pr['body']) if pr['body'] else ''
                html += f'''
                    <div class="pr-item">
                        <div class="pr-header-row">
                            <a href="{safe_url}" target="_blank" class="pr-link">#{pr['pr_number']}: {safe_title}</a>
                            <span class="pr-badge">{safe_categories}</span>
                        </div>
                        <div class="pr-meta">
                            <span>By @{safe_author}</span>
                            <span>&bull;</span>
                            <span>Created: {created_date}</span>
                            <span>&bull;</span>
                            <span>Branch: {safe_branch}</span>
                        </div>
                        {f'<div class="pr-body">{safe_body}</div>' if safe_body else ''}
                    </div>
                '''
            html += '</div></div>'
        html += '</div>'

    # General FIP-related PRs
    if general_prs:
        html += '<div class="prs-section"><h3>General FIP-Related PRs</h3>'
        html += '<div class="pr-list">'
        for pr in general_prs:
            created_date = datetime.fromisoformat(pr['created_at'].replace('Z', '+00:00')).strftime('%Y-%m-%d')
            safe_title = html_escape(pr['title'])
            safe_author = html_escape(pr['author'])
            safe_url = html_escape(pr['url'])
            safe_branch = html_escape(pr['branch'])
            safe_categories = html_escape(', '.join(pr['categories']))
            safe_body = html_escape(pr['body']) if pr['body'] else ''
            html += f'''
                <div class="pr-item">
                    <div class="pr-header-row">
                        <a href="{safe_url}" target="_blank" class="pr-link">#{pr['pr_number']}: {safe_title}</a>
                        <span class="pr-badge">{safe_categories}</span>
                    </div>
                    <div class="pr-meta">
                        <span>By @{safe_author}</span>
                        <span>&bull;</span>
                        <span>Created: {created_date}</span>
                        <span>&bull;</span>
                        <span>Branch: {safe_branch}</span>
                    </div>
                    {f'<div class="pr-body">{safe_body}</div>' if safe_body else ''}
                </div>
            '''
        html += '</div></div>'

    html += '</div>'
    return html


def main():
    print("Fetching open PRs from GitHub...")
    prs = fetch_open_prs()

    if not prs:
        print("No open PRs found or error occurred")
        return {}

    print(f"Found {len(prs)} open PR(s)")

    print("Processing PRs...")
    fip_prs = process_prs(prs)

    print(f"Processed {len(fip_prs)} PR(s)")

    # Count PRs by FIP
    fip_counts = {}
    for pr in fip_prs:
        for fip_num in pr['fip_numbers']:
            if fip_num != 'General':
                fip_counts[fip_num] = fip_counts.get(fip_num, 0) + 1

    print(f"PRs relate to {len(fip_counts)} unique FIP(s)")

    return {
        'prs': fip_prs,
        'prs_by_fip': fip_counts,
        'total_prs': len(fip_prs)
    }


if __name__ == '__main__':
    result = main()
    print(f"\nSummary:")
    print(f"Total PRs: {result.get('total_prs', 0)}")
    if result.get('prs_by_fip'):
        print(f"FIPs with PRs: {len(result['prs_by_fip'])}")
        for fip_num, count in sorted(result['prs_by_fip'].items()):
            print(f"  FIP-{fip_num}: {count} PR(s)")
