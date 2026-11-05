"""Read-focused Streamlit dashboard for Ignition Backup Manager V3.
Place beside ignition_backup_manager.py, live/, demo/, and ignition_audit.sqlite3.
Run: py -m streamlit run ignition_dashboard.py
"""
import difflib
import hashlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
LIVE, DEMO = ROOT / 'live', ROOT / 'demo'
DB = ROOT / 'ignition_audit.sqlite3'
MAX_READ = 8 * 1024 * 1024
MAX_ZIP_MEMBERS = 20000

st.set_page_config(page_title='Ignition Backup Manager', page_icon='⚙️', layout='wide')
st.markdown('''<style>
.block-container {padding-top: 1.8rem; max-width: 1450px;}
[data-testid="stMetric"] {background: rgba(128,128,128,.08); padding: 16px; border-radius: 12px;}
.small-note {opacity:.72; font-size:.9rem;}
</style>''', unsafe_allow_html=True)


def walk_files(base):
    if not base.is_dir():
        return {}, [f'Missing folder: {base}']
    result, errors = {}, []
    try:
        for p in base.rglob('*'):
            try:
                if p.is_file() and not p.is_symlink():
                    result[p.relative_to(base).as_posix()] = p
            except OSError as exc:
                errors.append(f'{p}: {exc}')
    except OSError as exc:
        errors.append(str(exc))
    return result, errors


def sha256_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_small(path):
    if path.stat().st_size > MAX_READ:
        raise ValueError(f'File larger than {MAX_READ // 1024 // 1024} MB preview limit')
    return path.read_bytes()


def json_object(data):
    return json.loads(data.decode('utf-8-sig'))


def normalized_json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def value_repr(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 350 else text[:347] + '...'


def json_changes(before, after, path='$'):
    changes = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            sub = f'{path}.{key}'
            if key not in before:
                changes.append(('ADDED', sub, '', value_repr(after[key])))
            elif key not in after:
                changes.append(('REMOVED', sub, value_repr(before[key]), ''))
            else:
                changes.extend(json_changes(before[key], after[key], sub))
    elif isinstance(before, list) and isinstance(after, list):
        for i in range(max(len(before), len(after))):
            sub = f'{path}[{i}]'
            if i >= len(before):
                changes.append(('ADDED', sub, '', value_repr(after[i])))
            elif i >= len(after):
                changes.append(('REMOVED', sub, value_repr(before[i]), ''))
            else:
                changes.extend(json_changes(before[i], after[i], sub))
    elif type(before) is not type(after) or before != after:
        changes.append(('MODIFIED', path, value_repr(before), value_repr(after)))
    return changes


def display_json_comparison(before_bytes, after_bytes, title):
    try:
        before, after = json_object(before_bytes), json_object(after_bytes)
        changes = json_changes(before, after)
    except (UnicodeError, ValueError, TypeError) as exc:
        st.error(f'Cannot parse JSON: {exc}')
        return
    st.subheader(title)
    if not changes:
        st.success('Semantically identical JSON (formatting and object key order ignored).')
    else:
        st.warning(f'{len(changes)} JSON property differences')
        st.dataframe(pd.DataFrame(changes, columns=['Change', 'JSON path', 'LIVE / Before', 'DEMO / After']), use_container_width=True, hide_index=True)
    with st.expander('Side-by-side JSON'):
        left, right = st.columns(2)
        left.caption('LIVE / Before')
        left.code(json.dumps(before, indent=2, ensure_ascii=False)[:60000], language='json')
        right.caption('DEMO / After')
        right.code(json.dumps(after, indent=2, ensure_ascii=False)[:60000], language='json')


def db_events():
    if not DB.is_file():
        return pd.DataFrame(columns=['id', 'detected_at', 'path', 'action', 'before_text', 'after_text', 'old_hash', 'new_hash']), None
    try:
        # Open read-only: dashboard never creates or modifies the audit database.
        with sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=3) as conn:
            df = pd.read_sql_query('SELECT id, detected_at, path, action, before_text, after_text, old_hash, new_hash FROM events ORDER BY id DESC', conn)
        return df, None
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        return pd.DataFrame(), str(exc)


def git_remote_branch():
    """Prefer the original repository, even when origin is an engineer fork."""
    for remote in ('upstream', 'origin'):
        cp = subprocess.run(['git', 'remote', 'get-url', remote], cwd=ROOT,
                            capture_output=True, text=True, timeout=10, check=False)
        if cp.returncode == 0 and 'Carl-tech-7/Ignition-Projects' in cp.stdout.replace('\\', '/'):
            return remote, f'{remote}/main'
    return None, None


def fetch_remote(remote):
    try:
        cp = subprocess.run(['git', 'fetch', '--no-tags', remote, 'main'], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=25, check=False)
        return None if cp.returncode == 0 else (cp.stderr.strip() or 'Git fetch failed')
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)


def git_remote_files(ref, query):
    try:
        cp = subprocess.run(['git', 'ls-tree', '-r', '--name-only', ref, '--', 'live', 'demo'],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=15, check=False)
        if cp.returncode:
            return [], cp.stderr.strip()
        return [name for name in cp.stdout.splitlines() if query.casefold() in name.casefold()], None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)


def git_log(ref):
    try:
        cp = subprocess.run(['git', 'log', ref, '-50', '--date=iso-strict', '--pretty=format:%h%x1f%ad%x1f%an%x1f%ae%x1f%s', '--', 'live', 'demo'], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, check=False)
        if cp.returncode:
            return [], cp.stderr.strip() or 'Git history unavailable'
        records = []
        for line in cp.stdout.splitlines():
            cells = line.split('\x1f', 4)
            if len(cells) == 5:
                records.append(dict(commit=cells[0], date=cells[1], author=cells[2], email=cells[3], message=cells[4]))
        return records, None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)



# Git metadata is evidence of commits, not proof of the person who edited a file.
# Git history is fetched from the original repository; no staging, commits, or pushes.
def git_file_history(ref, limit=500):
    command = [
        'git', 'log', ref, f'-{limit}', '--date=iso-strict',
        '--format=%x1e%H%x1f%ad%x1f%an%x1f%ae%x1f%s',
        '--name-status', '--no-renames', '--', 'live', 'demo'
    ]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', errors='replace',
                                timeout=40, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return pd.DataFrame(), str(exc)
    if result.returncode:
        return pd.DataFrame(), result.stderr.strip() or 'Unable to read Git history'

    records = []
    for block in result.stdout.split('\x1e'):
        block = block.lstrip('\r\n')
        if not block.strip():
            continue
        lines = block.splitlines()
        fields = lines[0].split('\x1f', 4)
        if len(fields) != 5:
            continue
        commit, date, author, email, message = fields
        for line in lines[1:]:
            if not line.strip() or '\t' not in line:
                continue
            status, file_path = line.split('\t', 1)
            if status not in ('A', 'M', 'D', 'T'):
                continue
            if not file_path.startswith(('live/', 'demo/')):
                continue
            records.append({
                'commit': commit, 'date': date, 'author': author,
                'email': email, 'message': message,
                'change': {'A': 'Added', 'M': 'Modified',
                           'D': 'Deleted', 'T': 'Type changed'}[status],
                'file': file_path,
                'json': file_path.lower().endswith('.json'),
            })
    return pd.DataFrame(records), None


def git_file_diff(commit, path):
    # The selected values originate from git log; no shell is involved.
    try:
        result = subprocess.run(
            ['git', 'show', '--format=', '--no-ext-diff', '--no-renames',
             '--unified=3', commit, '--', path],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=15, check=False,
        )
        if result.returncode:
            return None, result.stderr.strip() or 'Diff unavailable'
        return result.stdout[:100000], None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)

def zip_listing(path, json_only=True):
    with zipfile.ZipFile(path) as archive:
        infos = [i for i in archive.infolist() if not i.is_dir() and (not json_only or i.filename.lower().endswith('.json'))]
        if len(infos) > MAX_ZIP_MEMBERS:
            raise ValueError('Too many ZIP entries to display')
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            raise ValueError('Duplicate member names inside ZIP')
        return {i.filename: i.file_size for i in infos}


def zip_read_member(path, member):
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
        if info.file_size > MAX_READ:
            raise ValueError('ZIP member exceeds preview limit')
        with archive.open(info) as stream:
            data = stream.read(MAX_READ + 1)
        if len(data) > MAX_READ:
            raise ValueError('ZIP member exceeds preview limit')
        return data


st.title('⚙️ Ignition Backup Manager')
st.caption('V3 visual dashboard · LIVE / DEMO explorer · GitHub history auto-fetches every 30 seconds')

live_files, live_errors = walk_files(LIVE)
demo_files, demo_errors = walk_files(DEMO)
events, db_error = db_events()
all_files = sorted(set(live_files) | set(demo_files), key=str.casefold)

with st.sidebar:
    st.header('Workspace')
    st.code(str(ROOT), language=None)
    st.caption('The dashboard must be saved beside ignition_backup_manager.py.')
    if st.button('Refresh dashboard', use_container_width=True):
        st.rerun()
    st.divider()
    st.subheader('Audit scan')
    st.caption('Uses your existing V3 scanner. This updates the SQLite audit database, but never uploads to GitHub.')
    if st.button('Run audit scan', use_container_width=True):
        script = ROOT / 'ignition_backup_manager.py'
        if not script.is_file():
            st.error('ignition_backup_manager.py not found beside dashboard.')
        else:
            try:
                spec = importlib.util.spec_from_file_location('ignition_backup_manager_v3', script)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not hasattr(module, 'scan_changes'):
                    st.error('The existing program does not provide scan_changes().')
                else:
                    import contextlib
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        module.scan_changes()
                    st.code(output.getvalue() or 'Scan finished.', language=None)
                    st.info('Click Refresh dashboard to load new audit events.')
            except Exception as exc:
                st.error(f'Audit scan failed: {exc}')
    st.divider()
    st.info('GitHub history fetches automatically every 30 seconds. No automatic pushes or pulls.')

if live_errors or demo_errors:
    st.warning('Some folders or files could not be listed: ' + '; '.join((live_errors + demo_errors)[:3]))
if db_error:
    st.warning('Audit database could not be read: ' + db_error)

summary, explorer, comparison, zips, history, git_tab, authors_tab = st.tabs(['📊 Overview', '📁 Project explorer', '🔍 LIVE vs DEMO JSON', '🗜️ ZIP comparisons', '🕓 Audit history', '🌿 Git history', '👤 Who changed what?'])

with summary:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('LIVE files', len(live_files))
    c2.metric('DEMO files', len(demo_files))
    c3.metric('JSON files (both)', sum(x.lower().endswith('.json') for x in live_files) + sum(x.lower().endswith('.json') for x in demo_files))
    c4.metric('Recorded audit events', len(events))
    st.subheader('Project inventory')
    projects = sorted({p.split('/')[0] for p in all_files}, key=str.casefold)
    project_rows = []
    for p in projects:
        project_rows.append({'Project / top-level item': p, 'LIVE files': sum(k == p or k.startswith(p + '/') for k in live_files), 'DEMO files': sum(k == p or k.startswith(p + '/') for k in demo_files)})
    st.dataframe(pd.DataFrame(project_rows), use_container_width=True, hide_index=True)
    if not events.empty:
        st.subheader('Changes by action')
        st.bar_chart(events['action'].value_counts())
        try:
            trend = events.copy()
            trend['day'] = pd.to_datetime(trend['detected_at'], utc=True).dt.date
            counts = trend.groupby(['day', 'action']).size().unstack(fill_value=0)
            st.subheader('Changes over time')
            st.line_chart(counts)
        except (ValueError, TypeError):
            st.caption('Some event timestamps could not be plotted.')
    else:
        st.info('No audit events yet. The first scan creates a baseline; later scans record changes.')

with explorer:
    scope = st.radio('Folder', ['LIVE', 'DEMO'], horizontal=True)
    files = live_files if scope == 'LIVE' else demo_files
    search = st.text_input('Search filenames or project paths', placeholder='Dryers, view.json, tags...')
    only_json = st.checkbox('JSON files only', value=False)
    visible = [p for p in files if search.lower() in p.lower() and (not only_json or p.lower().endswith('.json'))]
    st.caption(f'{len(visible)} matching files')
    if visible:
        selected = st.selectbox('Choose a file', visible)
        path = files[selected]
        try:
            st.write(f'**Size:** {path.stat().st_size:,} bytes')
            if path.suffix.lower() == '.json':
                obj = json_object(read_small(path))
                st.json(obj, expanded=False)
            elif path.suffix.lower() in {'.txt', '.md', '.py', '.xml', '.csv'}:
                st.code(read_small(path).decode('utf-8-sig'), language=None)
            else:
                st.caption('Binary or unsupported file type. File content is not previewed.')
        except (OSError, ValueError, UnicodeError) as exc:
            st.error(f'Unable to preview file: {exc}')
    else:
        st.info('No files match your filters.')

with comparison:
    json_names = [p for p in all_files if p.lower().endswith('.json')]
    if not json_names:
        st.info('No JSON files found in LIVE or DEMO.')
    else:
        project_options = ['All projects'] + sorted({p.split('/')[0] for p in json_names}, key=str.casefold)
        project_filter = st.selectbox('Project filter', project_options)
        filtered = [p for p in json_names if project_filter == 'All projects' or p.split('/')[0] == project_filter]
        match_only = st.checkbox('Show only files present in both folders', value=True)
        if match_only:
            filtered = [p for p in filtered if p in live_files and p in demo_files]
        if filtered:
            selected = st.selectbox('JSON file to compare', filtered)
            if selected not in live_files:
                st.info('File exists only in DEMO.')
            elif selected not in demo_files:
                st.info('File exists only in LIVE.')
            else:
                try:
                    display_json_comparison(read_small(live_files[selected]), read_small(demo_files[selected]), selected)
                except (OSError, ValueError) as exc:
                    st.error(f'Cannot compare: {exc}')
        else:
            st.info('No matching JSON files for these filters.')

with zips:
    live_zips = sorted((p for p in live_files.values() if p.suffix.lower() == '.zip'), key=lambda p: p.as_posix().lower())
    demo_zips = sorted((p for p in demo_files.values() if p.suffix.lower() == '.zip'), key=lambda p: p.as_posix().lower())
    if not live_zips or not demo_zips:
        st.info('A ZIP archive is needed in both LIVE and DEMO to compare them.')
    else:
        a, b = st.columns(2)
        live_zip = a.selectbox('LIVE ZIP', live_zips, format_func=lambda p: p.relative_to(LIVE).as_posix())
        demo_zip = b.selectbox('DEMO ZIP', demo_zips, format_func=lambda p: p.relative_to(DEMO).as_posix())
        st.caption('ZIP filenames can differ. Members are matched by their internal relative paths.')
        if st.button('List JSON members and differences'):
            try:
                lnames, dnames = zip_listing(live_zip), zip_listing(demo_zip)
                st.session_state['zip_pair'] = (str(live_zip), str(demo_zip))
                st.session_state['zip_members'] = sorted(set(lnames) | set(dnames), key=str.casefold)
            except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
                st.error(f'Cannot inspect ZIP: {exc}')
        if st.session_state.get('zip_pair') == (str(live_zip), str(demo_zip)):
            members = st.session_state.get('zip_members', [])
            st.caption(f'{len(members)} JSON member paths found')
            if members:
                selected_member = st.selectbox('ZIP JSON member', members)
                try:
                    left_names, right_names = zip_listing(live_zip), zip_listing(demo_zip)
                    if selected_member not in left_names:
                        st.info('This JSON member exists only in DEMO ZIP.')
                    elif selected_member not in right_names:
                        st.info('This JSON member exists only in LIVE ZIP.')
                    else:
                        display_json_comparison(zip_read_member(live_zip, selected_member), zip_read_member(demo_zip, selected_member), selected_member)
                except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
                    st.error(f'ZIP member comparison failed: {exc}')

with history:
    if events.empty:
        st.info('No recorded audit changes. Run Option 6 in your terminal or use Run audit scan in the sidebar.')
    else:
        kinds = sorted(events['action'].dropna().unique().tolist())
        actions = st.multiselect('Change types', kinds, default=kinds)
        json_only = st.checkbox('Only JSON events (including ZIP JSON members)', value=False, key='history_json')
        query = st.text_input('Filter audit paths', placeholder='Dryers, tags.json, ...')
        filtered = events[events['action'].isin(actions) & events['path'].str.contains(query, case=False, regex=False, na=False)]
        if json_only:
            filtered = filtered[filtered['path'].str.lower().str.endswith('.json')]
        st.dataframe(filtered[['id', 'detected_at', 'path', 'action']], use_container_width=True, hide_index=True)
        if not filtered.empty:
            selected_id = st.selectbox('Inspect event', filtered['id'].tolist(), format_func=lambda i: f'#{i} · {filtered.loc[filtered.id == i, "path"].iloc[0]}')
            record = filtered.loc[filtered.id == selected_id].iloc[0]
            before = record['before_text'] if pd.notna(record['before_text']) else None
            after = record['after_text'] if pd.notna(record['after_text']) else None
            if before is None and after is None:
                st.info('Text snapshot unavailable (binary or large file).')
            elif str(record['path']).lower().endswith('.json'):
                try:
                    display_json_comparison((before or '{}').encode(), (after or '{}').encode(), str(record['path']))
                except (ValueError, TypeError) as exc:
                    st.error(str(exc))
            else:
                diff = difflib.unified_diff((before or '').splitlines(), (after or '').splitlines(), fromfile='BEFORE', tofile='AFTER', lineterm='')
                st.code('\n'.join(list(diff)[:500]) or 'No textual differences', language='diff')
        export = filtered[['id', 'detected_at', 'path', 'action', 'old_hash', 'new_hash']].to_csv(index=False).encode('utf-8-sig')
        st.download_button('Download filtered audit CSV', export, file_name='ignition_audit_filtered.csv', mime='text/csv')

@st.fragment(run_every="30s")
def render_git_activity():
    remote, ref = git_remote_branch()
    if remote is None:
        with git_tab:
            st.error('Cannot find the original Carl-tech-7/Ignition-Projects remote. Check git remote -v.')
        with authors_tab:
            st.error('Cannot find the original repository remote.')
        return
    fetch_error = fetch_remote(remote)
    with git_tab:
        st.caption(f'Git source: {ref} · Automatic fetch every 30 seconds while dashboard is open')
        if fetch_error:
            st.warning(f'Latest fetch failed; showing last fetched history: {fetch_error}')
        if st.button('Fetch latest GitHub history', key='fetch_now'):
            err = fetch_remote(remote)
            if err:
                st.error(err)
            else:
                st.success('Fetched the latest GitHub history.')
    with authors_tab:
        st.caption(f'Git source: {ref} · No local LIVE/DEMO files are changed by fetching')
        query = st.text_input('Check whether a file exists on GitHub main', value='motor_test_02.json', key='remote_file_query')
        if query.strip():
            matches, lookup_error = git_remote_files(ref, query.strip())
            if lookup_error:
                st.warning(lookup_error)
            elif matches:
                st.success('Found on GitHub main: ' + ', '.join(matches[:10]))
            else:
                st.info('No matching file on the original repository main branch. Check its exact filename, branch, or unmerged pull request.')
    with git_tab:
        records, error = git_log(ref)
        if error:
            st.warning(error)
        elif not records:
            st.info('No Git commits for LIVE or DEMO yet.')
        else:
            st.dataframe(pd.DataFrame(records), use_container_width=True, hide_index=True)
            st.caption('Git author identifies the commit identity, not necessarily the person who edited the original Ignition project.')


    with authors_tab:
        st.subheader('Who changed what? — Git commit authors')
        st.caption('Reads the original GitHub repository remote branch affecting LIVE and DEMO. Names and emails are Git author metadata, not verified editor identities. Git fetch updates history only; no push or local file checkout is performed.')
        max_commits = st.select_slider('Recent commits to inspect', options=[50, 100, 250, 500, 1000], value=500)
        git_changes, git_error = git_file_history(ref, max_commits)
        if git_error:
            st.error(git_error)
        elif git_changes.empty:
            st.info('No committed file changes found in fetched Git history for live/ and demo/. Uncommitted edits are not attributed.')
        else:
            a, b, c = st.columns(3)
            a.metric('Recorded file changes', len(git_changes))
            b.metric('Distinct Git authors', git_changes['email'].str.casefold().nunique())
            c.metric('JSON file changes', int(git_changes['json'].sum()))

            names = sorted(git_changes['author'].unique(), key=str.casefold)
            chosen_authors = st.multiselect('Git authors', names, default=names)
            folder = st.selectbox('Folder', ['LIVE and DEMO', 'LIVE', 'DEMO'])
            json_only = st.checkbox('JSON files only', value=False, key='authors_json_only')
            search = st.text_input('Search filenames or commit messages', placeholder='Dryers, view.json, tag path...')

            visible = git_changes[git_changes['author'].isin(chosen_authors)].copy()
            if folder != 'LIVE and DEMO':
                visible = visible[visible['file'].str.startswith(folder.lower() + '/')]
            if json_only:
                visible = visible[visible['json']]
            if search:
                visible = visible[
                    visible['file'].str.contains(search, case=False, regex=False) |
                    visible['message'].str.contains(search, case=False, regex=False)
                ]
            st.markdown('#### Changes by Git author')
            if visible.empty:
                st.info('No commits match these filters.')
            else:
                author_counts = (visible.groupby(['author', 'email']).size()
                                 .reset_index(name='file changes')
                                 .sort_values('file changes', ascending=False))
                st.bar_chart(author_counts.set_index('author')['file changes'])
                st.dataframe(author_counts, use_container_width=True, hide_index=True)
                st.markdown('#### File change history')
                st.dataframe(visible[['date', 'file', 'change', 'author', 'email', 'commit', 'message']],
                             use_container_width=True, hide_index=True)
                csv_data = visible.drop(columns=['json']).to_csv(index=False).encode('utf-8-sig')
                st.download_button('Download Git author report (CSV)', csv_data,
                                   file_name='ignition_git_authors.csv', mime='text/csv')

                files = sorted(visible['file'].unique(), key=str.casefold)
                chosen_file = st.selectbox('Inspect a specific file', files)
                file_history = visible[visible['file'] == chosen_file]
                st.dataframe(file_history[['date', 'change', 'author', 'email', 'commit', 'message']],
                             use_container_width=True, hide_index=True)
                commit_choices = file_history['commit'].tolist()
                chosen_commit = st.selectbox('Select commit to inspect', commit_choices,
                                             format_func=lambda commit: next(
                                                 f'{commit[:10]} · {row.author} · {row.change}'
                                                 for row in file_history.itertuples()
                                                 if row.commit == commit))
                if st.button('Show committed file diff'):
                    diff, diff_error = git_file_diff(chosen_commit, chosen_file)
                    if diff_error:
                        st.warning(diff_error)
                    else:
                        st.code(diff or 'No textual diff (possibly a binary file).', language='diff')
                if chosen_file.lower().endswith('.zip'):
                    st.caption('Git identifies the author who committed the ZIP file. It cannot identify who changed individual JSON members inside that ZIP.')
        st.info('Important: Git authorship can be configured locally and is not independently verified. This view does not identify changes made in Ignition Designer unless those changes were committed to Git.')

render_git_activity()

st.divider()
st.caption('Safety: browsing and comparisons do not modify LIVE/DEMO files. The optional audit scan writes only to the existing audit database. No Git push is performed by this dashboard.')
