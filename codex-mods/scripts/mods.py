"""Codex command-hook adapter for Nate Herk's mods (MIT).

SQLite serializes reservations across processes. No model requests or network
calls are made. Screen masking and provider cache expiry are not emulated.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import sys
import time

WINDOW = 30 * 60
RESERVATION = 5 * 60
RETENTION = 24 * 60 * 60
PRIVATE = re.compile(r'(^|[/\\])(?:\.env[^/\\]*|\.ssh|\.aws|\.gnupg|credentials|secrets?|payroll|invoices?|contracts?|budgets?|forecasts?|tax)(?:[/\\.]|$)', re.I)
SIZES = {'S': 1, 'M': 2, 'L': 3}


def patch_paths(command, cwd):
    if not isinstance(command, str):
        raise ValueError('Patch command is missing')
    lines = command.strip().splitlines()
    if len(lines) < 3 or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        raise ValueError('Patch envelope could not be checked')
    paths = []
    for line in lines[1:-1]:
        if line.startswith(('*** Add File: ', '*** Update File: ', '*** Delete File: ', '*** Move to: ')):
            raw = line.split(': ', 1)[1]
            if not raw or '\x00' in raw or raw.startswith(('http:', 'https:', 'file:')):
                raise ValueError('Patch file path could not be checked')
            paths.append(os.path.normcase(str((Path(cwd) / raw).resolve())))
        elif line.startswith('*** ') and line != '*** End of File':
            raise ValueError('Unknown patch directive')
    if not paths:
        raise ValueError('No patch targets could be checked')
    return sorted(set(paths))


def deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                  'permissionDecision': 'deny',
                                  'permissionDecisionReason': reason}}


def context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def outcome(response):
    if isinstance(response, dict):
        if response.get('isError') or response.get('error'):
            return False
        code = response.get('exit_code')
        if type(code) is int:
            return code == 0
        text = response.get('output', '')
        if 'content' in response:
            text = '\n'.join(x.get('text', '') for x in response['content'] if isinstance(x, dict))
    else:
        text = response if isinstance(response, str) else ''
    if text.startswith('Success. Updated the following files:'):
        return True
    if text.startswith(('Failed', 'Error', 'error:')):
        return False
    return None  # Keep the short reservation; never invent a completed edit.


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('MODS_DATA', str(Path.home() / '.codex/mods-data')))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        database = self.directory / 'mods.sqlite3'
        self.db = sqlite3.connect(database, timeout=10)
        if os.name != 'nt':
            database.chmod(0o600)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, cwd TEXT, transcript TEXT, touched REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS claims (
                path TEXT PRIMARY KEY, session TEXT NOT NULL, call TEXT,
                reserved REAL NOT NULL DEFAULT 0, edited REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS goals (
                session TEXT PRIMARY KEY, plan TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def recording(self, enabled):
        with self.db:
            if enabled:
                self.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('recording', 'true'))
            else:
                self.db.execute('DELETE FROM settings WHERE key=?', ('recording',))
        return {'recording': enabled, 'screen_masking': False,
                'notice': 'File/tool safeguards only. Existing screen text is not masked.'}

    def private(self, path):
        p = str(Path(path).resolve())
        return bool(PRIVATE.search(p)) or any(
            part.casefold() in p.casefold() for part in self.setting('private_paths', []))

    def hook(self, event):
        name = event.get('hook_event_name')
        sid = event.get('session_id')
        if not isinstance(sid, str) or not sid:
            if name == 'PreToolUse':
                return deny('Mods cannot check this operation without a session ID.')
            raise ValueError('Session ID is missing')
        now = time.time()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM claims WHERE reserved < ? AND edited < ?',
                            (now - RESERVATION, now - WINDOW))
            self.db.execute('DELETE FROM sessions WHERE touched < ?', (now - RETENTION,))
            self.db.execute('DELETE FROM goals WHERE session NOT IN (SELECT id FROM sessions)')
            if name == 'SessionEnd':
                self.db.execute('DELETE FROM claims WHERE session=?', (sid,))
                self.db.execute('DELETE FROM sessions WHERE id=?', (sid,))
                self.db.execute('DELETE FROM goals WHERE session=?', (sid,))
                return {}
            self.db.execute('''INSERT INTO sessions VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET cwd=excluded.cwd,
                transcript=COALESCE(excluded.transcript, sessions.transcript), touched=excluded.touched''',
                            (sid, event.get('cwd'), event.get('transcript_path'), now))
            rec = self.setting('recording', False)
            if name in ('SessionStart', 'UserPromptSubmit'):
                note = 'Codex Mods: use the mods skill for goal, recording, guard, board, and handoff commands.'
                if rec:
                    note += ' Recording safeguards are ON. Do not print private names, keys, business figures or plans. This is a prompt instruction, not screen masking.'
                return context(name, note)
            tool = event.get('tool_name', '')
            data = event.get('tool_input')
            if not isinstance(data, dict):
                if name == 'PreToolUse' and (rec or tool == 'apply_patch'):
                    return deny('Mods cannot check this tool input.')
                return {}
            if name == 'PreToolUse' and rec and tool != 'apply_patch':
                if not self.control_call(tool, data):
                    return deny('Recording safeguards block shell, read, image and external tools. Screen masking is unavailable. Use the mods control CLI or ask the user to turn recording off.')
            if tool != 'apply_patch':
                return {}
            try:
                paths = patch_paths(data.get('command'), event.get('cwd') or '.')
            except ValueError as exc:
                return deny(str(exc)) if name == 'PreToolUse' else {}
            if name == 'PreToolUse':
                if rec and any(self.private(p) for p in paths):
                    return deny('Recording safeguards keep this private file closed.')
                if not self.setting('guard', True):
                    return {}
                call = event.get('tool_use_id')
                if not isinstance(call, str) or not call:
                    return deny('Mods cannot identify this edit call.')
                for path in paths:
                    row = self.db.execute('''SELECT session FROM claims WHERE path=?
                        AND (session!=? OR (reserved>? AND call IS NOT ?))
                        AND (reserved>? OR edited>?)''',
                                          (path, sid, now - RESERVATION, event.get('tool_use_id'),
                                           now - RESERVATION, now - WINDOW)).fetchone()
                    if row:
                        return deny('Another session reserved or changed this file. Use a separate worktree, or have the user resolve the collision. No file was changed.')
                for path in paths:
                    self.db.execute('''INSERT INTO claims VALUES (?, ?, ?, ?, 0)
                        ON CONFLICT(path) DO UPDATE SET session=excluded.session,
                        call=excluded.call, reserved=excluded.reserved''',
                                    (path, sid, event.get('tool_use_id'), now))
            elif name == 'PostToolUse':
                result = outcome(event.get('tool_response'))
                if result is not None:
                    for path in paths:
                        self.db.execute('''UPDATE claims SET reserved=0, edited=CASE WHEN ? THEN ? ELSE edited END
                            WHERE path=? AND session=? AND call IS ?''',
                                        (result, now, path, sid, event.get('tool_use_id')))
            return {}

    @staticmethod
    def control_call(tool, data):
        if tool not in ('Bash', 'exec_command'):
            return False
        if any(data.get(key) is not None for key in ('shell', 'env')):
            return False
        raw = data.get('command', data.get('cmd', ''))
        if not isinstance(raw, str) or re.search(r'[;$`&|<>\n\r]', raw):
            return False
        try:
            argv = shlex.split(raw)
        except ValueError:
            return False
        interpreter = shutil.which(argv[0]) if argv else None
        return (len(argv) >= 3 and interpreter is not None
                and Path(interpreter).resolve() == Path(sys.executable).resolve()
                and Path(argv[1]).resolve() == Path(__file__).resolve()
                and ((len(argv) == 3 and argv[2] in ('board', 'status'))
                     or (len(argv) == 4 and argv[2] in ('rec', 'guard') and argv[3] in ('on', 'off', 'status'))))

    def goal(self, sid, action, value=None, tasks=None):
        if not sid:
            raise ValueError('Use --session or run within a Codex session')
        with self.db:
            self.db.execute('INSERT INTO sessions VALUES (?, ?, NULL, ?) ON CONFLICT(id) DO UPDATE SET touched=excluded.touched',
                            (sid, str(Path.cwd()), time.time()))
            if action == 'clear':
                self.db.execute('DELETE FROM goals WHERE session=?', (sid,))
                return {'status': 'cleared'}
            if action == 'set':
                if not value or not isinstance(tasks, list) or not tasks:
                    raise ValueError('A goal title and nonempty task list are required')
                for task in tasks:
                    if not isinstance(task, dict) or not isinstance(task.get('title'), str) or not task['title'] or task.get('size') not in SIZES:
                        raise ValueError('Every task needs a title and S, M or L size')
                plan = {'title': value, 'started': time.time(),
                        'tasks': [{'title': t['title'], 'size': t['size'], 'status': 'pending'} for t in tasks]}
            else:
                row = self.db.execute('SELECT plan FROM goals WHERE session=?', (sid,)).fetchone()
                if not row:
                    raise ValueError('No goal plan exists')
                plan = json.loads(row[0])
                if action not in ('start', 'done') or not str(value).isdigit() or not 1 <= int(value) <= len(plan['tasks']):
                    raise ValueError('Task number does not exist')
                task = plan['tasks'][int(value) - 1]
                if task['status'] == 'done' and action == 'start':
                    raise ValueError('A completed task cannot be restarted')
                task['status'] = 'done' if action == 'done' else 'running'
            self.db.execute('INSERT OR REPLACE INTO goals VALUES (?, ?)', (sid, json.dumps(plan)))
        return self.goals(sid)

    def goals(self, sid):
        row = self.db.execute('SELECT plan FROM goals WHERE session=?', (sid,)).fetchone()
        if not row:
            return {'status': 'no plan'}
        p = json.loads(row[0])
        total = sum(SIZES[t['size']] for t in p['tasks'])
        done = sum(SIZES[t['size']] for t in p['tasks'] if t['status'] == 'done')
        percent = round(100 * done / total)
        result = {'percent': percent, 'bar': '[' + '#' * (percent // 5) + '-' * (20 - percent // 5) + ']'}
        if not self.setting('recording', False):
            result.update(title=p['title'], tasks=p['tasks'])
        return result

    def board(self):
        rows = []
        rec = self.setting('recording', False)
        for sid, cwd, transcript, _ in self.db.execute('SELECT * FROM sessions ORDER BY touched DESC'):
            row = {'session': sid[:8], 'goal': self.goals(sid)}
            if not rec:
                row['cwd'] = cwd
                row['usage_status'] = 'unavailable'
                if transcript:
                    try:
                        # Bounded tail: no prompt, tool or reply content is output.
                        with Path(transcript).open('rb') as stream:
                            stream.seek(0, 2)
                            stream.seek(max(0, stream.tell() - 256 * 1024))
                            tail = stream.read().decode('utf-8', errors='replace')
                        for line in reversed(tail.splitlines()):
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            payload = obj.get('payload', {})
                            if obj.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                                usage = payload.get('info', {}).get('total_token_usage', {})
                                for key in ('input_tokens', 'cached_input_tokens', 'output_tokens'):
                                    if type(usage.get(key)) is int:
                                        row[key] = usage[key]
                                row['usage_status'] = 'measured' if 'input_tokens' in row else 'unavailable'
                                break
                    except OSError:
                        pass
            rows.append(row)
        return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', default=os.environ.get('CODEX_THREAD_ID'))
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('hook')
    commands.add_parser('board')
    commands.add_parser('status')
    for command in ('rec', 'guard'):
        q = commands.add_parser(command)
        q.add_argument('mode', choices=('on', 'off', 'status'))
    q = commands.add_parser('goal')
    q.add_argument('action', choices=('set', 'start', 'done', 'clear', 'show'))
    q.add_argument('value', nargs='?')
    q.add_argument('--tasks', help='JSON list of title and size objects')
    args = p.parse_args()
    store = None
    try:
        store = Store()
        if args.command == 'hook':
            result = store.hook(json.load(sys.stdin))
        elif args.command == 'rec':
            result = store.recording(args.mode == 'on') if args.mode != 'status' else {'recording': store.setting('recording', False), 'screen_masking': False}
        elif args.command == 'guard':
            if args.mode != 'status':
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('guard', json.dumps(args.mode == 'on')))
            result = {'guard': store.setting('guard', True), 'coverage': 'apply_patch file edits only; arbitrary shell writes are not tracked'}
        elif args.command == 'goal':
            result = store.goals(args.session) if args.action == 'show' else store.goal(args.session, args.action, args.value, json.loads(args.tasks) if args.tasks else None)
        elif args.command == 'board':
            result = store.board()
        else:
            result = {'recording': store.setting('recording', False), 'guard': store.setting('guard', True),
                      'screen_masking': False, 'cache_keep_warm': False, 'board': store.board()}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, sqlite3.Error, OSError) as exc:
        if args.command == 'hook':
            print('Codex Mods could not verify its state; operation blocked.', file=sys.stderr)
            return 2
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == '__main__':
    sys.exit(main())
