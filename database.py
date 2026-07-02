"""
Dahaka — SQLite Database Layer
All config, domains, automation steps, jobs, and results stored here.
"""
import sqlite3
import uuid
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "dahaka.db"

def get_conn():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        concurrent_browsers INTEGER DEFAULT 10,
        proxy_file TEXT DEFAULT 'google_valid_proxies.txt',
        proxy_mode TEXT DEFAULT 'local_file',
        headless_mode INTEGER DEFAULT 0,
        page_load_timeout INTEGER DEFAULT 10,
        element_timeout_short INTEGER DEFAULT 3,
        element_timeout INTEGER DEFAULT 5,
        proxy_max_retries INTEGER DEFAULT 0,
        current_emails_ttl INTEGER DEFAULT 10,
        display_limit INTEGER DEFAULT 10,
        status_update_interval INTEGER DEFAULT 1,
        turbo_mode INTEGER DEFAULT 1,
        browser_work_mode TEXT DEFAULT 'same_domain_diff_email',
        multi_domain_email_mode TEXT DEFAULT 'same_email',
        browser_domain_map TEXT DEFAULT '{}',
        css_check_enabled INTEGER DEFAULT 1
    );

    INSERT OR IGNORE INTO config (id) VALUES (1);

    CREATE TABLE IF NOT EXISTS rotated_proxies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        port INTEGER NOT NULL,
        enabled INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        letter TEXT NOT NULL DEFAULT '',
        enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS automation_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_id INTEGER NOT NULL,
        step_order INTEGER NOT NULL,
        action TEXT NOT NULL,
        selector_type TEXT DEFAULT '',
        selector_value TEXT DEFAULT '',
        input_value TEXT DEFAULT '',
        timeout INTEGER DEFAULT 5,
        on_success TEXT DEFAULT 'continue',
        on_failure TEXT DEFAULT 'proxy_error',
        description TEXT DEFAULT '',
        FOREIGN KEY (domain_id) REFERENCES domains(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        status TEXT DEFAULT 'pending',
        domain_id INTEGER,
        total_emails INTEGER DEFAULT 0,
        processed INTEGER DEFAULT 0,
        success INTEGER DEFAULT 0,
        failure INTEGER DEFAULT 0,
        started_at TEXT,
        finished_at TEXT,
        FOREIGN KEY (domain_id) REFERENCES domains(id)
    );

    CREATE TABLE IF NOT EXISTS job_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        email TEXT NOT NULL,
        domain_id INTEGER,
        result TEXT DEFAULT 'pending',
        proxy_used TEXT DEFAULT '',
        checked_at TEXT,
        FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
        FOREIGN KEY (domain_id) REFERENCES domains(id)
    );

    CREATE TABLE IF NOT EXISTS email_lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS email_list_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        domain_results TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (list_id) REFERENCES email_lists(id) ON DELETE CASCADE
    );
    """)
    conn.commit()
    conn.close()

    # Migrate: add new columns to existing config table if missing
    conn = get_conn()
    existing = [r[1] for r in conn.execute("PRAGMA table_info(config)").fetchall()]
    migrations = [
        ("browser_work_mode", "TEXT DEFAULT 'same_domain_diff_email'"),
        ("multi_domain_email_mode", "TEXT DEFAULT 'same_email'"),
        ("browser_domain_map", "TEXT DEFAULT '{}'"),
        ("css_check_enabled", "INTEGER DEFAULT 1"),
    ]
    for col, typedef in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE config ADD COLUMN {col} {typedef}")
    conn.commit()
    conn.close()

# ── Config ──────────────────────────────────────────────────────────
def get_config():
    conn = get_conn()
    row = conn.execute("SELECT * FROM config WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else {}

def save_config(data: dict):
    conn = get_conn()
    cols = ['concurrent_browsers','proxy_file','proxy_mode',
            'headless_mode','page_load_timeout','element_timeout_short','element_timeout',
            'proxy_max_retries','current_emails_ttl','display_limit','status_update_interval',
            'turbo_mode','browser_work_mode','multi_domain_email_mode','browser_domain_map',
            'css_check_enabled']
    sets = ", ".join(f"{c}=?" for c in cols if c in data)
    vals = [data[c] for c in cols if c in data]
    if sets and vals:
        conn.execute(f"UPDATE config SET {sets} WHERE id=1", vals)
        conn.commit()
    conn.close()

# ── Domains ─────────────────────────────────────────────────────────
def get_domains():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM domains ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_domain(domain_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def add_domain(name: str, url: str, letter: str = ''):
    conn = get_conn()
    c = conn.execute("INSERT INTO domains (name, url, letter) VALUES (?,?,?)", (name, url, letter))
    domain_id = c.lastrowid
    conn.commit()
    conn.close()
    return domain_id

def update_domain(domain_id: int, data: dict):
    conn = get_conn()
    cols = ['name','url','letter','enabled']
    sets = ", ".join(f"{c}=?" for c in cols if c in data)
    vals = [data[c] for c in cols if c in data]
    if sets and vals:
        vals.append(domain_id)
        conn.execute(f"UPDATE domains SET {sets} WHERE id=?", vals)
        conn.commit()
    conn.close()

def delete_domain(domain_id: int):
    conn = get_conn()
    # Clear referencing rows first (jobs/job_results don't have ON DELETE CASCADE)
    conn.execute("DELETE FROM job_results WHERE domain_id=?", (domain_id,))
    conn.execute("UPDATE jobs SET domain_id=NULL WHERE domain_id=?", (domain_id,))
    conn.execute("DELETE FROM automation_steps WHERE domain_id=?", (domain_id,))
    conn.execute("DELETE FROM domains WHERE id=?", (domain_id,))
    conn.commit()
    conn.close()

# ── Automation Steps ────────────────────────────────────────────────
def get_steps(domain_id: int):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM automation_steps WHERE domain_id=? ORDER BY step_order", (domain_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_steps(domain_id: int, steps: list):
    """Replace all steps for a domain with the given list."""
    conn = get_conn()
    conn.execute("DELETE FROM automation_steps WHERE domain_id=?", (domain_id,))
    for i, step in enumerate(steps):
        conn.execute("""INSERT INTO automation_steps
            (domain_id, step_order, action, selector_type, selector_value, input_value, timeout, on_success, on_failure, description)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (domain_id, i+1, step.get('action',''), step.get('selector_type',''),
             step.get('selector_value',''), step.get('input_value',''),
             step.get('timeout',5), step.get('on_success','continue'),
             step.get('on_failure','proxy_error'), step.get('description','')))
    conn.commit()
    conn.close()

# ── Jobs ────────────────────────────────────────────────────────────
def create_job(domain_id: int, total_emails: int):
    job_id = str(uuid.uuid4())[:8]
    conn = get_conn()
    conn.execute("INSERT INTO jobs (id, status, domain_id, total_emails, started_at) VALUES (?,?,?,?,?)",
                 (job_id, 'running', domain_id, total_emails, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return job_id

def update_job(job_id: str, data: dict):
    conn = get_conn()
    cols = ['status','processed','success','failure','finished_at']
    sets = ", ".join(f"{c}=?" for c in cols if c in data)
    vals = [data[c] for c in cols if c in data]
    if sets and vals:
        vals.append(job_id)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", vals)
        conn.commit()
    conn.close()

def get_jobs(limit=50):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_job(job_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

# ── Results ─────────────────────────────────────────────────────────
def add_result(job_id: str, email: str, domain_id: int, result: str, proxy_used: str = ''):
    conn = get_conn()
    conn.execute("INSERT INTO job_results (job_id, email, domain_id, result, proxy_used, checked_at) VALUES (?,?,?,?,?,?)",
                 (job_id, email, domain_id, result, proxy_used, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_results(job_id: str):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM job_results WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Rotated Proxies ─────────────────────────────────────────────────
def get_rotated_proxies():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM rotated_proxies ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_rotated_proxy(ip: str, port: int):
    conn = get_conn()
    c = conn.execute("INSERT INTO rotated_proxies (ip, port) VALUES (?,?)", (ip, port))
    pid = c.lastrowid
    conn.commit()
    conn.close()
    return pid

def delete_rotated_proxy(proxy_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM rotated_proxies WHERE id=?", (proxy_id,))
    conn.commit()
    conn.close()

def toggle_rotated_proxy(proxy_id: int, enabled: int):
    conn = get_conn()
    conn.execute("UPDATE rotated_proxies SET enabled=? WHERE id=?", (enabled, proxy_id))
    conn.commit()
    conn.close()

# ── Seed Defaults ───────────────────────────────────────────────────
def seed_amazon_default():
    """Seed Amazon domain + automation steps if no domains exist."""
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
    if count > 0:
        conn.close()
        return
    # Add Amazon domain
    c = conn.execute("INSERT INTO domains (name, url, letter) VALUES (?,?,?)",
                     ("Amazon", "https://brandregistry.amazon.com/", "A"))
    did = c.lastrowid
    # Add automation steps
    steps = [
        (did, 1, 'navigate', '', '', '{{DOMAIN_URL}}', 10, 'continue', 'proxy_error', 'Navigate to Amazon'),
        (did, 2, 'check_url', '', '', 'amazon', 3, 'continue', 'proxy_error', 'Verify domain in URL'),
        (did, 3, 'type', 'id', 'ap_email', '{{EMAIL}}', 3, 'continue', 'proxy_error', 'Enter email'),
        (did, 4, 'click', 'id', 'continue', '', 5, 'continue', 'proxy_error', 'Click continue'),
        (did, 5, 'wait', 'css', 'body', '', 5, 'continue', 'proxy_error', 'Wait for page load'),
        (did, 6, 'check_element', 'id', 'auth-error-message-box', '', 3, 'mark_invalid', 'continue', 'Check error box → invalid'),
        (did, 7, 'check_element', 'id', 'ap_change_login_claim', '', 3, 'mark_valid', 'continue', 'Check login claim → valid'),
        (did, 8, 'check_element', 'id', 'a-autoid-0-announce', '', 3, 'mark_valid', 'proxy_error', 'Check alt valid element'),
    ]
    conn.executemany("""INSERT INTO automation_steps
        (domain_id, step_order, action, selector_type, selector_value, input_value, timeout, on_success, on_failure, description)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", steps)
    conn.commit()
    conn.close()

# ── Email Lists ─────────────────────────────────────────────────────
def create_email_list(name: str, description: str = ''):
    conn = get_conn()
    c = conn.execute(
        "INSERT INTO email_lists (name, description) VALUES (?,?)",
        (name, description)
    )
    list_id = c.lastrowid
    conn.commit()
    conn.close()
    return list_id

def get_email_lists():
    """Get all email lists with summary stats."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM email_lists ORDER BY id DESC").fetchall()
    lists = []
    for row in rows:
        d = dict(row)
        lid = d['id']
        # Count total emails
        total = conn.execute("SELECT COUNT(*) FROM email_list_entries WHERE list_id=?", (lid,)).fetchone()[0]
        d['total_emails'] = total
        # Count domain results stats
        entries = conn.execute("SELECT domain_results FROM email_list_entries WHERE list_id=?", (lid,)).fetchall()
        # Get all domains for column counting
        domains = conn.execute("SELECT id, name FROM domains ORDER BY id").fetchall()
        d['total_domains'] = len(domains)
        d['domain_names'] = [dict(dom)['name'] for dom in domains]
        d['domain_ids'] = [dict(dom)['id'] for dom in domains]
        # Calculate checked/valid/invalid counts
        total_cells = total * len(domains) if domains else 0
        checked = 0
        valid = 0
        invalid = 0
        for entry in entries:
            try:
                dr = json.loads(entry['domain_results'] or '{}')
            except (json.JSONDecodeError, TypeError):
                dr = {}
            for dom in domains:
                did_str = str(dict(dom)['id'])
                val = dr.get(did_str, '')
                if val == '1':
                    checked += 1
                    valid += 1
                elif val == '0':
                    checked += 1
                    invalid += 1
        d['total_cells'] = total_cells
        d['checked'] = checked
        d['valid'] = valid
        d['invalid'] = invalid
        d['unchecked'] = total_cells - checked
        d['progress'] = round((checked / total_cells * 100), 1) if total_cells > 0 else 0
        lists.append(d)
    conn.close()
    return lists

def get_email_list(list_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM email_lists WHERE id=?", (list_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_email_list(list_id: int, data: dict):
    conn = get_conn()
    cols = ['name', 'description']
    sets = ", ".join(f"{c}=?" for c in cols if c in data)
    vals = [data[c] for c in cols if c in data]
    if sets and vals:
        vals.append(datetime.now().isoformat())
        vals.append(list_id)
        conn.execute(f"UPDATE email_lists SET {sets}, updated_at=? WHERE id=?", vals)
        conn.commit()
    conn.close()

def delete_email_list(list_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM email_list_entries WHERE list_id=?", (list_id,))
    conn.execute("DELETE FROM email_lists WHERE id=?", (list_id,))
    conn.commit()
    conn.close()

def add_emails_to_list(list_id: int, emails: list):
    """Bulk insert emails into a list. Skips duplicates within the list."""
    conn = get_conn()
    existing = set()
    rows = conn.execute("SELECT email FROM email_list_entries WHERE list_id=?", (list_id,)).fetchall()
    for r in rows:
        existing.add(r['email'].lower())
    added = 0
    for email in emails:
        email = email.strip()
        if email and email.lower() not in existing:
            conn.execute(
                "INSERT INTO email_list_entries (list_id, email, domain_results) VALUES (?,?,?)",
                (list_id, email, '{}')
            )
            existing.add(email.lower())
            added += 1
    if added:
        conn.execute("UPDATE email_lists SET updated_at=? WHERE id=?",
                     (datetime.now().isoformat(), list_id))
    conn.commit()
    conn.close()
    return added

def remove_emails_from_list(list_id: int, entry_ids: list):
    """Remove specific entries from a list."""
    conn = get_conn()
    for eid in entry_ids:
        conn.execute("DELETE FROM email_list_entries WHERE id=? AND list_id=?", (eid, list_id))
    conn.execute("UPDATE email_lists SET updated_at=? WHERE id=?",
                 (datetime.now().isoformat(), list_id))
    conn.commit()
    conn.close()

def get_email_list_entries(list_id: int):
    """Get all entries for a list, with parsed domain results."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM email_list_entries WHERE list_id=? ORDER BY id",
        (list_id,)
    ).fetchall()
    domains = conn.execute("SELECT id, name FROM domains ORDER BY id").fetchall()
    conn.close()
    entries = []
    domain_list = [dict(d) for d in domains]
    for row in rows:
        d = dict(row)
        try:
            d['domain_results'] = json.loads(d['domain_results'] or '{}')
        except (json.JSONDecodeError, TypeError):
            d['domain_results'] = {}
        entries.append(d)
    return {'entries': entries, 'domains': domain_list}

def update_email_entry_result(entry_id: int, domain_id: int, result: str):
    """Update a single domain result for an email entry.
    result should be '1' (valid), '0' (invalid), or '' (unchecked)."""
    conn = get_conn()
    row = conn.execute("SELECT domain_results, list_id FROM email_list_entries WHERE id=?", (entry_id,)).fetchone()
    if row:
        try:
            dr = json.loads(row['domain_results'] or '{}')
        except (json.JSONDecodeError, TypeError):
            dr = {}
        dr[str(domain_id)] = result
        conn.execute("UPDATE email_list_entries SET domain_results=? WHERE id=?",
                     (json.dumps(dr), entry_id))
        conn.execute("UPDATE email_lists SET updated_at=? WHERE id=?",
                     (datetime.now().isoformat(), row['list_id']))
        conn.commit()
    conn.close()

def bulk_update_results(list_id: int, domain_id: int, results_map: dict):
    """Bulk update domain results for a list.
    results_map: {email: '1'|'0'}"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, email, domain_results FROM email_list_entries WHERE list_id=?",
        (list_id,)
    ).fetchall()
    updated = 0
    for row in rows:
        email = row['email'].lower()
        if email in results_map:
            try:
                dr = json.loads(row['domain_results'] or '{}')
            except (json.JSONDecodeError, TypeError):
                dr = {}
            dr[str(domain_id)] = results_map[email]
            conn.execute("UPDATE email_list_entries SET domain_results=? WHERE id=?",
                         (json.dumps(dr), row['id']))
            updated += 1
    if updated:
        conn.execute("UPDATE email_lists SET updated_at=? WHERE id=?",
                     (datetime.now().isoformat(), list_id))
    conn.commit()
    conn.close()
    return updated

def sync_domain_added(domain_id: int):
    """When a new domain is added, no action needed — JSON dict just won't have the key yet,
    which means 'unchecked'. This is a no-op but kept for symmetry."""
    pass

def sync_domain_removed(domain_id: int):
    """When a domain is deleted, remove its key from all email_list_entries domain_results."""
    conn = get_conn()
    rows = conn.execute("SELECT id, domain_results FROM email_list_entries").fetchall()
    did_str = str(domain_id)
    for row in rows:
        try:
            dr = json.loads(row['domain_results'] or '{}')
        except (json.JSONDecodeError, TypeError):
            dr = {}
        if did_str in dr:
            del dr[did_str]
            conn.execute("UPDATE email_list_entries SET domain_results=? WHERE id=?",
                         (json.dumps(dr), row['id']))
    conn.commit()
    conn.close()

def export_email_list_pipe(list_id: int) -> str:
    """Export list in pipe-delimited format:
    email|domain1|domain2|...
    test@gmail.com|1|0||1|..."""
    conn = get_conn()
    lst = conn.execute("SELECT * FROM email_lists WHERE id=?", (list_id,)).fetchone()
    if not lst:
        conn.close()
        return ''
    domains = conn.execute("SELECT id, name FROM domains ORDER BY id").fetchall()
    entries = conn.execute(
        "SELECT email, domain_results FROM email_list_entries WHERE list_id=? ORDER BY id",
        (list_id,)
    ).fetchall()
    conn.close()

    lines = []
    # Header
    header = 'email|' + '|'.join(dict(d)['name'] for d in domains)
    lines.append(header)
    # Data
    for entry in entries:
        try:
            dr = json.loads(entry['domain_results'] or '{}')
        except (json.JSONDecodeError, TypeError):
            dr = {}
        row_parts = [entry['email']]
        for dom in domains:
            did_str = str(dict(dom)['id'])
            row_parts.append(dr.get(did_str, ''))
        lines.append('|'.join(row_parts))
    return '\n'.join(lines)

def import_emails_from_job(list_id: int, job_id: str):
    """Import results from a completed job into an email list."""
    conn = get_conn()
    # Get job results
    results = conn.execute(
        "SELECT email, domain_id, result FROM job_results WHERE job_id=?",
        (job_id,)
    ).fetchall()
    if not results:
        conn.close()
        return 0
    # Get existing entries in the list
    existing = {}
    rows = conn.execute(
        "SELECT id, email, domain_results FROM email_list_entries WHERE list_id=?",
        (list_id,)
    ).fetchall()
    for r in rows:
        existing[r['email'].lower()] = {'id': r['id'], 'dr': r['domain_results']}
    added = 0
    updated = 0
    for res in results:
        email = res['email'].strip()
        domain_id = res['domain_id']
        result_val = '1' if res['result'] in ('valid', 'Present') else '0' if res['result'] in ('invalid', 'Not Present') else ''
        email_lower = email.lower()
        if email_lower not in existing:
            # Add new entry
            dr = {str(domain_id): result_val} if domain_id else {}
            conn.execute(
                "INSERT INTO email_list_entries (list_id, email, domain_results) VALUES (?,?,?)",
                (list_id, email, json.dumps(dr))
            )
            eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            existing[email_lower] = {'id': eid, 'dr': json.dumps(dr)}
            added += 1
        else:
            # Update existing entry
            if domain_id:
                try:
                    dr = json.loads(existing[email_lower]['dr'] or '{}')
                except (json.JSONDecodeError, TypeError):
                    dr = {}
                dr[str(domain_id)] = result_val
                conn.execute("UPDATE email_list_entries SET domain_results=? WHERE id=?",
                             (json.dumps(dr), existing[email_lower]['id']))
                existing[email_lower]['dr'] = json.dumps(dr)
                updated += 1
    conn.execute("UPDATE email_lists SET updated_at=? WHERE id=?",
                 (datetime.now().isoformat(), list_id))
    conn.commit()
    conn.close()
    return added + updated

def update_email_entry_result_by_email(list_id: int, email: str, domain_id: int, result: str):
    """Find an entry by email+list_id and update its domain result.
    Used by process_email() to auto-update lists during jobs."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, domain_results FROM email_list_entries WHERE list_id=? AND LOWER(email)=LOWER(?)",
        (list_id, email)
    ).fetchone()
    if row:
        try:
            dr = json.loads(row['domain_results'] or '{}')
        except (json.JSONDecodeError, TypeError):
            dr = {}
        dr[str(domain_id)] = result
        conn.execute("UPDATE email_list_entries SET domain_results=? WHERE id=?",
                     (json.dumps(dr), row['id']))
        conn.execute("UPDATE email_lists SET updated_at=? WHERE id=?",
                     (datetime.now().isoformat(), list_id))
        conn.commit()
    conn.close()

def get_emails_for_job(list_id: int, mode: str = 'all', domain_ids: list = None):
    """Get emails from a list for the job page.
    mode='all' → all emails
    mode='unverified' → only emails that have empty result for ANY of the given domain_ids
    Returns list of email strings."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT email, domain_results FROM email_list_entries WHERE list_id=? ORDER BY id",
        (list_id,)
    ).fetchall()
    conn.close()
    emails = []
    for row in rows:
        if mode == 'all':
            emails.append(row['email'])
        elif mode == 'unverified' and domain_ids:
            try:
                dr = json.loads(row['domain_results'] or '{}')
            except (json.JSONDecodeError, TypeError):
                dr = {}
            # Include email if it has no result for ANY of the specified domains
            for did in domain_ids:
                if dr.get(str(did), '') == '':
                    emails.append(row['email'])
                    break
        else:
            emails.append(row['email'])
    return emails

if __name__ == '__main__':
    init_db()
    seed_amazon_default()
    print(f"Database initialized at {DB_PATH}")
    print(f"Config: {get_config()}")
    print(f"Domains: {get_domains()}")
