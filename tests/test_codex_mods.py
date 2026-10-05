import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import shlex


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mods', ROOT / 'codex-mods/scripts/mods.py')
mods = importlib.util.module_from_spec(spec)
sys.modules['mods'] = mods
spec.loader.exec_module(mods)


class ModsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = mods.Store(self.root / 'state')
        self.addCleanup(self.store.close)

    def event(self, sid='a', event='PreToolUse', tool='apply_patch', command=None):
        return {'session_id': sid, 'cwd': str(self.root), 'hook_event_name': event,
                'tool_name': tool, 'tool_use_id': sid + '-call',
                'tool_input': {'command': command or '*** Begin Patch\n*** Add File: one.txt\n+ok\n*** End Patch'}}

    def test_collision_blocks_whole_patch_and_releases_on_end(self):
        self.assertEqual(self.store.hook(self.event()), {})
        answer = self.store.hook(self.event('b'))
        self.assertEqual(answer['hookSpecificOutput']['permissionDecision'], 'deny')
        self.store.hook(self.event(event='SessionEnd'))
        self.assertEqual(self.store.hook(self.event('b')), {})

    def test_concurrent_reservation_excludes_second_connection(self):
        other = mods.Store(self.root / 'state')
        self.addCleanup(other.close)
        self.store.hook(self.event())
        self.assertIn('deny', json.dumps(other.hook(self.event('b'))))

    def test_failed_patch_does_not_become_edit_history(self):
        self.store.hook(self.event())
        e = self.event(event='PostToolUse')
        e['tool_response'] = {'exit_code': 1, 'output': 'failed'}
        self.store.hook(e)
        self.assertEqual(self.store.hook(self.event('b')), {})

    def test_successful_patch_tracks_move_and_delete(self):
        patch = '*** Begin Patch\n*** Update File: old.txt\n*** Move to: new.txt\n@@\n-a\n+b\n*** Delete File: gone.txt\n*** End Patch'
        e = self.event(command=patch)
        self.store.hook(e)
        e['hook_event_name'] = 'PostToolUse'
        e['tool_response'] = 'Success. Updated the following files:\nM new.txt\nD gone.txt'
        self.store.hook(e)
        paths = {x[0] for x in self.store.db.execute('select path from claims')}
        self.assertEqual(paths, {os.path.normcase(str(self.root / x)) for x in ['old.txt', 'new.txt', 'gone.txt']})

    def test_resume_does_not_erase_edit_history(self):
        self.store.hook(self.event())
        self.store.hook(self.event(event='SessionStart'))
        self.assertIn('deny', json.dumps(self.store.hook(self.event('b'))))

    def test_recording_blocks_shell_and_sensitive_paths(self):
        self.store.recording(True)
        self.assertIn('deny', json.dumps(self.store.hook(self.event(tool='Bash', command='cat .env'))))
        e = self.event(command='*** Begin Patch\n*** Add File: nested/../.env\n+secret\n*** End Patch')
        self.assertIn('deny', json.dumps(self.store.hook(e)))
        self.store.recording(False)
        self.assertEqual(self.store.hook(e), {})

    def test_recording_blocks_mcp_and_unknown_read_tools(self):
        self.store.recording(True)
        for name in ['mcp__gmail__read', 'read_file', 'view_image']:
            self.assertIn('deny', json.dumps(self.store.hook(self.event(tool=name))))

    def test_recording_does_not_print_goal_title(self):
        self.store.goal('a', 'set', 'Private Client', [{'title': 'Payroll', 'size': 'L'}])
        self.store.recording(True)
        self.assertNotIn('Private', json.dumps(self.store.goals('a')))

    def test_goal_weight_and_invalid_updates(self):
        self.store.goal('a', 'set', 'Ship', [{'title': 'First', 'size': 'S'}, {'title': 'Second', 'size': 'L'}])
        self.store.goal('a', 'done', '1')
        self.assertEqual(self.store.goals('a')['percent'], 25)
        with self.assertRaises(ValueError):
            self.store.goal('a', 'done', '9')
        self.store.goal('a', 'clear')
        self.assertEqual(self.store.goals('a'), {'status': 'no plan'})

    def test_tokens_not_price_or_cache_expiry(self):
        transcript = self.root / 'rollout.jsonl'
        transcript.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 100, 'cached_input_tokens': 75, 'output_tokens': 10}}}}) + '\n', encoding='utf-8')
        e = self.event(event='SessionStart')
        e['transcript_path'] = str(transcript)
        self.store.hook(e)
        row = self.store.board()[0]
        self.assertEqual(row['cached_input_tokens'], 75)
        self.assertNotIn('price', row)
        self.assertNotIn('ttl', row)

    def test_malformed_patch_is_not_silently_allowed(self):
        self.assertIn('deny', json.dumps(self.store.hook(self.event(command='not a patch'))))

    def test_recording_control_cannot_execute_shell_suffix(self):
        self.store.recording(True)
        runtime = str(ROOT / 'codex-mods/scripts/mods.py')
        for suffix in ['; cat .env', ' && cat .env', ' $(cat .env)', '\ncat .env']:
            e = self.event(tool='Bash', command=f'python3 {runtime} rec status{suffix}')
            self.assertIn('deny', json.dumps(self.store.hook(e)))

    def test_recording_allows_exact_control_command(self):
        self.store.recording(True)
        runtime = str(ROOT / 'codex-mods/scripts/mods.py')
        command = f'{shlex.quote(sys.executable)} {shlex.quote(runtime)} rec status'
        self.assertEqual(self.store.hook(self.event(tool='Bash', command=command)), {})

    def test_same_session_overlap_does_not_drop_first_reservation(self):
        first = self.event()
        self.store.hook(first)
        second = self.event()
        second['tool_use_id'] = 'second'
        self.assertIn('deny', json.dumps(self.store.hook(second)))
        second['hook_event_name'] = 'PostToolUse'
        second['tool_response'] = {'exit_code': 1}
        self.store.hook(second)
        self.assertIn('deny', json.dumps(self.store.hook(self.event('b'))))

    def test_recording_rejects_impostor_interpreter_and_shell_override(self):
        self.store.recording(True)
        runtime = str(ROOT / 'codex-mods/scripts/mods.py')
        for tool, command, extra in [
            ('Bash', f'/tmp/python3 {runtime} rec status', {}),
            ('exec_command', f'python3 {runtime} rec status', {'shell': '/tmp/arbitrary'}),
        ]:
            e = self.event(tool=tool, command=command)
            e['tool_input'].update(extra)
            self.assertIn('deny', json.dumps(self.store.hook(e)))

    def test_missing_call_id_is_unknown_not_a_shared_reservation(self):
        for value in [None, '', 123]:
            e = self.event()
            e['tool_use_id'] = value
            self.assertIn('deny', json.dumps(self.store.hook(e)))


if __name__ == '__main__':
    unittest.main()
