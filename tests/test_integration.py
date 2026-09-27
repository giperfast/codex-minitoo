import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import minitoo
from usage_limits import normalize


class IntegrationTests(unittest.TestCase):
    def test_final_design_reply_requires_completed_transfer(self):
        reply = 'delegate: rx[13]: 01 09 00 04 bd 55 13 01 05 00 38 01 02\n'
        start = 'sendFrames: count=95 delay=40ms\n'
        done = 'sendFrames: done elapsed=4000ms\n'
        self.assertTrue(minitoo.image_upload_completed(start + done + reply, 95))
        self.assertFalse(minitoo.image_upload_completed(reply + start + done, 95))
        self.assertFalse(minitoo.image_upload_completed(start + reply, 95))
        self.assertFalse(minitoo.image_upload_completed(start + done + reply, 94))

    def test_ack_parser_handles_split_frames_and_ignores_transmitted_bytes(self):
        text = ('tx[0]: 01 08 00 04 8b 55 01 00 00 ed 00 02\n'
                'delegate: rx[5]: 01 07 00 04 8b\n'
                'delegate: rx[18]: 55 00 01 ec 00 02 01 08 00 04 8b 55 01 01 00 ee 00 02\n')
        self.assertEqual(minitoo.image_acknowledgements(text), (True, {1}))
        self.assertEqual(minitoo.image_acknowledgements('delegate: rx[12]: 01 08 00 04 8b 55 01 00 00 ff 00 02'), (False, set()))

    def test_upload_requires_acknowledgement_for_every_block(self):
        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as temp, patch.object(minitoo, 'RUN', Path(temp)), patch.object(minitoo.time, 'sleep'):
                run = Path(temp)
                (run / 'screens').mkdir()
                (run / 'screens/working.jpg').write_bytes(b'x' * 600)
                cache = run / 'chunks'
                cache.mkdir()
                (run / 'screen-cache.json').write_text(json.dumps({'folder': str(cache)}))
                log = run / 'bluetooth.log'
                def reply(payload):
                    body = bytes([len(payload)+2, 0]) + bytes(payload)
                    frame = bytes([1]) + body + sum(body).to_bytes(2, 'little') + bytes([2])
                    with log.open('a') as stream:
                        stream.write(f'delegate: rx[{len(frame)}]: {frame.hex(" ")}\n')
                def send(command):
                    if command.startswith('raw '):
                        reply([4, 0x8b, 0x55, 0, 1])
                    else:
                        for index in (range(3) if complete else (2,)):
                            reply([4, 0x8b, 0x55, 1, index, 0])
                with patch.object(minitoo, 'fifo_write', side_effect=send), patch.object(minitoo.time, 'monotonic', side_effect=range(100)):
                    if complete:
                        minitoo.upload_screen('working')
                    else:
                        with self.assertRaisesRegex(TimeoutError, '1/3'):
                            minitoo.upload_screen('working')

    def test_usage_prefers_codex_bucket_and_clamps_remaining(self):
        result = normalize({'rateLimits': {'primary': {'usedPercent': 90}},
            'rateLimitsByLimitId': {'codex': {
                'primary': {'usedPercent': 8, 'windowDurationMins': 300, 'resetsAt': 123},
                'secondary': {'usedPercent': 105, 'windowDurationMins': 10080}}}}, now=10)
        self.assertEqual(result['windows'][0], {'label': '5H', 'remaining': 92, 'resets_at': 123})
        self.assertEqual(result['windows'][1]['remaining'], 0)
        self.assertEqual(result['windows'][1]['label'], 'WEEK')

    def test_missing_usage_is_unknown(self):
        self.assertIsNone(normalize({})['windows'][0]['remaining'])

    def test_unchanged_status_refreshes_after_one_minute(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(minitoo, 'RUN', Path(temp)), patch.object(minitoo, 'display') as display:
            event = {'session_id': 'a', 'hook_event_name': 'PreToolUse'}
            minitoo.update(event, 1)
            minitoo.update(event, 30)
            self.assertEqual(display.call_count, 1)
            minitoo.update(event, 62)
            self.assertEqual(display.call_count, 2)
    def test_image_header_uses_cells_and_big_endian_jpeg_length(self):
        blob = minitoo.image_blob(b'jpeg' * 100)
        self.assertEqual(blob[:7], bytes([0x23, 1, 3, 0xe8, 8, 10, 1]))
        self.assertEqual(blob[7:11], b'\x00\x00\x01\x90')
        self.assertEqual(blob[11:], b'jpeg' * 100)
    def test_firmware_byte_limit(self):
        command = bytes.fromhex(minitoo.notification('Я' * 100).removeprefix('raw '))
        self.assertEqual(command[:3], bytes([0x50, 0x14, 128]))
        self.assertEqual(command[3:].decode(), 'Я' * 64)

    def test_waiting_session_survives_other_session_stop(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(minitoo, 'RUN', Path(temp)), patch.object(minitoo, 'display') as display:
            def send(session, event, now):
                minitoo.update({'session_id': session, 'hook_event_name': event}, now)
            send('a', 'PermissionRequest', 1)
            send('b', 'UserPromptSubmit', 2)
            send('b', 'Stop', 3)
            display.assert_called_once_with('waiting')
            send('a', 'PreToolUse', 4)
            self.assertEqual(display.call_args.args, ('working',))
            send('a', 'Stop', 5)
            self.assertEqual(display.call_args.args, ('done',))

    def test_failed_send_is_retried(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(minitoo, 'RUN', Path(temp)), patch.object(minitoo, 'display', side_effect=[RuntimeError('offline'), None]) as display:
            event = {'session_id': 'a', 'hook_event_name': 'UserPromptSubmit'}
            minitoo.update(event, 1)
            minitoo.update(event, 2)
            self.assertEqual(display.call_count, 2)
            self.assertEqual(json.loads((Path(temp) / 'state.json').read_text())['sent'], 'working')

    def test_existing_hooks_preserved_and_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'hooks.json'
            original = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'existing'}]}]}}
            path.write_text(json.dumps(original))
            minitoo.install_hooks(path)
            first = path.read_text()
            minitoo.install_hooks(path)
            self.assertEqual(first, path.read_text())
            self.assertEqual(json.loads(first)['hooks']['Stop'][0], original['hooks']['Stop'][0])

    def test_stale_fifo_times_out(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(minitoo, 'RUN', Path(temp)):
            import os
            os.mkfifo(Path(temp) / 'commands.fifo')
            with self.assertRaises(RuntimeError):
                minitoo.fifo_write('quit', timeout=.1)

    def test_image_timeout_preserves_existing_helper_for_next_attempt(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(minitoo, 'RUN', Path(temp)), patch.object(minitoo, 'config', return_value={}), patch.object(minitoo, 'refresh_limits'), patch.object(minitoo, 'upload_screen', side_effect=[TimeoutError('no reply'), None]) as upload, patch.object(minitoo, 'start') as start, patch.object(minitoo, 'fifo_write') as write:
            fifo = Path(temp) / 'commands.fifo'
            import os
            os.mkfifo(fifo)
            with self.assertRaises(TimeoutError):
                minitoo.display('working')
            self.assertTrue(fifo.exists())
            minitoo.display('done')
            self.assertEqual(upload.call_count, 2)
            start.assert_not_called()
            write.assert_not_called()

    def test_missing_helper_starts_before_upload(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(minitoo, 'RUN', Path(temp)), patch.object(minitoo, 'config', return_value={}), patch.object(minitoo, 'refresh_limits'), patch.object(minitoo, 'upload_screen') as upload, patch.object(minitoo, 'start') as start:
            minitoo.display('working')
            start.assert_called_once_with()
            upload.assert_called_once_with('working')


if __name__ == '__main__':
    unittest.main()
