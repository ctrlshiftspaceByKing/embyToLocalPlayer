"""Playlist progress regression tests, without loading the user's config or log hooks.

Run: python3 -m unittest discover -s tests -v
Real mpv + local Plex HTTP endpoint: ETLP_TEST_REAL_MPV=1 python3 -m unittest discover -s tests -v
"""
import ast
import configparser
import importlib.util
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def load_definitions(path, names, namespace):
    # These modules initialize user configuration and redirect stdout on import.
    # Compile the actual production definitions with isolated dependencies instead.
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if getattr(node, 'name', '') in names]
    assert len(nodes) == len(names), (path, names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / path), 'exec'), namespace)


class PropertyError(Exception):
    pass


class ScriptedMpv:
    """Advance one deterministic mpv sample per polling sleep."""
    def __init__(self, frames):
        self.frames = frames
        self.index = 0
        self.title_reads = 0

    def sleep(self, _seconds):
        self.index += 1
        self.title_reads = 0

    def on_event(self, _name):
        return lambda callback: callback

    def command(self, command, prop):
        if self.index >= len(self.frames):
            raise BrokenPipeError('mpv closed')
        frame = self.frames[self.index]
        if isinstance(frame, Exception):
            raise frame
        title, position, *changed_title = frame
        if prop == 'media-title':
            self.title_reads += 1
            return changed_title[0] if changed_title and self.title_reads > 1 else title
        if prop == 'time-pos':
            return position
        if prop == 'duration':
            return 120
        raise AssertionError((command, prop))


def make_environment():
    raw = configparser.ConfigParser(interpolation=None)
    raw.read_dict({'dev': {}, 'emby': {'update_progress': 'yes'}, 'playlist': {}})
    configs = types.SimpleNamespace(raw=raw, ini_str_split=lambda *a, **kw: [], script_proxy='')
    ns = dict(configs=configs, logger=Mock(), queue=queue, threading=threading, time=time,
              os=os, json=json, urllib=urllib, ssl_context=None, MPV=object, MPVError=PropertyError,
              EmbyApiThin=lambda data: None, sync_third_party_for_eps=Mock(),
              prefetch_data={'on': True, 'stop_sec_dict': {}}, requests_urllib=Mock(return_value=''))
    load_definitions('utils/net_tools.py', {'change_plex_play_position', 'update_server_playback_progress'}, ns)
    load_definitions('utils/players.py', {'stop_sec_mpv', 'playlist_add_mpv'}, ns)
    ns['stop_sec_func_dict'] = {'mpv': ns['stop_sec_mpv'], 'iina': ns['stop_sec_mpv']}
    load_definitions('utils/player_manager.py', {'BaseInit', 'BaseManager'}, ns)
    return ns


def make_manager(ns, count=3):
    manager = ns['BaseManager']({}, player_name='mpv')
    manager.playlist_data = {
        f'S01E{i:02}': dict(server='plex', basename=f'episode{i}.wav', file_path=f'/episode{i}.wav',
                            media_title=f'S01E{i:02}', media_path=f'/episode{i}.wav', sub_file='',
                            start_sec=0, total_sec=120, duration=120000, rating_key=str(i),
                            scheme='http', netloc='127.0.0.1:1', api_key='test-token', client_id='test-client')
        for i in range(1, count + 1)
    }
    return manager


class PlaylistProgressTests(unittest.TestCase):
    def setUp(self):
        self.ns = make_environment()
        self.manager = make_manager(self.ns)

    def run_frames(self, frames):
        mpv = ScriptedMpv(frames)
        self.ns['time'] = types.SimpleNamespace(sleep=mpv.sleep)
        self.manager.player_kwargs = {'mpv': mpv}
        self.manager.update_playlist_time_loop()

    def requests(self):
        return [call.kwargs['params'] for call in self.ns['requests_urllib'].call_args_list
                if call.args[0].endswith('/:/timeline')]

    def test_each_episode_submitted_at_switch_and_current_on_close(self):
        self.run_frames([('S01E01', 40), ('S01E01', 119), ('S01E02', 1),
                         ('S01E02', 118), ('S01E03', 1), ('S01E03', 45)])
        self.assertEqual([(r['ratingKey'], r['time']) for r in self.requests()], [('1', 117000), ('2', 116000)])
        self.manager.update_playback_for_eps()
        self.assertEqual([(r['ratingKey'], r['time']) for r in self.requests()],
                         [('1', 117000), ('2', 116000), ('3', 43000)])
        self.manager.update_playback_for_eps()
        self.assertEqual(len(self.requests()), 3)

    def test_zero_and_unavailable_positions_at_transition(self):
        self.run_frames([('S01E01', 0), ('S01E01', 90), ('S01E02', None),
                         ('S01E02', 0), ('S01E02', 50)])
        self.manager.update_playback_for_eps()
        self.assertEqual([(r['ratingKey'], r['time']) for r in self.requests()], [('1', 88000), ('2', 48000)])

    def test_zero_length_skipped_episode_is_not_reported(self):
        self.run_frames([('S01E01', 0), ('S01E02', 50)])
        self.manager.update_playback_for_eps()
        self.assertEqual([r['ratingKey'] for r in self.requests()], ['2'])

    def test_title_change_during_ipc_does_not_mix_episode_and_position(self):
        self.run_frames([('S01E01', 90), ('S01E01', 1, 'S01E02'), ('S01E02', 50)])
        self.manager.update_playback_for_eps()
        self.assertEqual([(r['ratingKey'], r['time']) for r in self.requests()], [('1', 88000), ('2', 48000)])

    def test_temporary_property_error_does_not_end_tracking(self):
        self.run_frames([('S01E01', 100), PropertyError('temporarily unavailable'),
                         ('S01E02', 110), ('S01E03', 40)])
        self.manager.update_playback_for_eps()
        self.assertEqual([r['ratingKey'] for r in self.requests()], ['1', '2', '3'])

    def test_revisited_episode_reports_new_position(self):
        self.run_frames([('S01E01', 80), ('S01E02', 90), ('S01E01', 40)])
        self.manager.update_playback_for_eps()
        self.assertEqual([(r['ratingKey'], r['time']) for r in self.requests()],
                         [('1', 78000), ('2', 88000), ('1', 38000)])

    def test_failed_switch_submission_is_retried_without_losing_later_episodes(self):
        original = self.ns['update_server_playback_progress']
        attempts = []

        def fail_once(stop_sec, data):
            attempts.append(data['rating_key'])
            if len(attempts) == 1:
                raise ConnectionError('test failure')
            return original(stop_sec=stop_sec, data=data)

        self.ns['update_server_playback_progress'] = fail_once
        self.run_frames([('S01E01', 100), ('S01E02', 110), ('S01E03', 40)])
        self.assertEqual([r['ratingKey'] for r in self.requests()], ['2'])
        self.manager.update_playback_for_eps()
        self.assertEqual(attempts, ['1', '2', '1', '3'])
        self.assertEqual([r['ratingKey'] for r in self.requests()], ['2', '1', '3'])

    def test_network_submission_does_not_block_sampling(self):
        entered, release = threading.Event(), threading.Event()
        original = self.ns['update_server_playback_progress']

        def slow_submit(stop_sec, data):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release network')
            return original(stop_sec=stop_sec, data=data)

        def collect(stop_sec_only, on_episode_end, **kwargs):
            on_episode_end('S01E01', 100, 120)
            self.assertTrue(entered.wait(2))
            on_episode_end('S01E02', 110, 120)
            release.set()
            return {'S01E01': 100, 'S01E02': 110, 'S01E03': 40}, {}

        self.ns['update_server_playback_progress'] = slow_submit
        self.ns['stop_sec_func_dict']['mpv'] = collect
        self.manager.update_playlist_time_loop()
        self.manager.update_playback_for_eps()
        self.assertEqual([r['ratingKey'] for r in self.requests()], ['1', '2', '3'])

    def test_disabled_progress_setting_is_respected(self):
        self.ns['configs'].raw.set('emby', 'update_progress', 'no')
        self.run_frames([('S01E01', 100), ('S01E02', 50)])
        self.manager.update_playback_for_eps()
        self.assertEqual(self.requests(), [])

    def test_single_episode_close_is_unchanged(self):
        self.run_frames([('S01E01', 60)])
        self.assertEqual(self.requests(), [])
        self.manager.update_playback_for_eps()
        self.assertEqual(self.requests()[0]['time'], 58000)

    def test_single_position_api_remains_compatible(self):
        mpv = ScriptedMpv([('S01E01', 60), ('S01E02', 40)])
        self.ns['time'] = types.SimpleNamespace(sleep=mpv.sleep)
        callback = Mock()
        self.assertEqual(self.ns['stop_sec_mpv'](mpv, on_episode_end=callback), 40)
        callback.assert_not_called()

    def test_non_mpv_player_keeps_existing_api(self):
        self.manager.player_name = 'vlc'
        collect = Mock(return_value={'S01E01': 60})
        self.ns['stop_sec_func_dict']['vlc'] = collect
        self.manager.update_playlist_time_loop()
        collect.assert_called_once_with(stop_sec_only=False)
        self.manager.update_playback_for_eps()
        self.assertEqual(self.requests()[0]['time'], 58000)


@unittest.skipUnless(os.environ.get('ETLP_TEST_REAL_MPV') == '1' and shutil.which('mpv') and os.name != 'nt',
                     'set ETLP_TEST_REAL_MPV=1 to run real mpv integration')
class RealMpvProgressTests(unittest.TestCase):
    def test_three_episodes_natural_transitions_then_quit(self):
        ns = make_environment()
        load_definitions('utils/net_tools.py', {'safe_url', 'requests_urllib'}, ns)
        spec = importlib.util.spec_from_file_location('test_mpv_jsonipc', ROOT / 'utils/python_mpv_jsonipc.py')
        ipc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ipc)
        ns['MPVError'] = ipc.MPVError
        manager = make_manager(ns)
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                if urllib.parse.unquote(parts.path) == '/:/timeline':
                    requests.append(urllib.parse.parse_qs(parts.query))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        def wait_until(predicate, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.05)
            print('\nProgress diagnostics:', ns['logger'].mock_calls[-15:], flush=True)
            print('Collector errors:', errors, flush=True)
            print('mpv log:', (Path(temp) / 'mpv.log').read_text(errors='replace')[-1000:], flush=True)
            self.fail('Timed out waiting for mpv/progress: ' + repr(requests))

        with tempfile.TemporaryDirectory(dir=os.environ.get('PI_SCRATCH_DIR')) as temp:
            paths = []
            for i, ep in enumerate(manager.playlist_data.values(), 1):
                path = Path(temp) / f'episode{i}.wav'
                with wave.open(str(path), 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(8000)
                    audio.writeframes(b'\0\0' * (120 * 8000))
                ep.update(media_path=str(path), file_path=str(path))
                paths.append(str(path))
            socket_path = str(Path(temp) / 'mpv.sock')
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            for ep in manager.playlist_data.values():
                ep['netloc'] = f'127.0.0.1:{server.server_port}'
            mpv = None
            collector = None
            errors = []
            with open(Path(temp) / 'mpv.log', 'w') as log:
                process = subprocess.Popen([
                    'mpv', '--no-config', '--vo=null', '--ao=null', '--idle=yes', '--pause=yes',
                    '--speed=20', '--audio-pitch-correction=no', '--force-media-title=S01E01',
                    '--input-ipc-server=' + socket_path, paths[0]], stdout=log, stderr=log)
                try:
                    wait_until(lambda: Path(socket_path).exists())
                    mpv = ipc.MPV(start_mpv=False, ipc_socket=socket_path)
                    mpv.is_iina = False
                    mpv.is_mpvnet = False
                    manager.player_kwargs = {'mpv': mpv}
                    data = dict(next(iter(manager.playlist_data.values())), mount_disk_mode=False)
                    ns['playlist_add_mpv'](mpv, data, eps_data=list(manager.playlist_data.values()))

                    def collect():
                        try:
                            manager.update_playlist_time_loop()
                            manager.update_playback_for_eps()
                        except Exception as exc:
                            errors.append(exc)

                    collector = threading.Thread(target=collect, daemon=True)
                    collector.start()
                    mpv.command('set_property', 'pause', False)
                    wait_until(lambda: len(mpv.command('get_property', 'playlist')) == 3)
                    wait_until(lambda: len(requests) >= 2)
                    self.assertIsNone(process.poll(), 'First two episodes must report before mpv exits')
                    self.assertEqual([r['ratingKey'][0] for r in requests], ['1', '2'])
                    wait_until(lambda: mpv.command('get_property', 'media-title') == 'S01E03'
                               and (mpv.command('get_property', 'time-pos') or 0) >= 45)
                    time.sleep(0.6)  # Allow the production sampler to retain the current position.
                    self.assertEqual(len(requests), 2, 'Current episode is flushed on quit')
                    mpv.command('quit')
                    process.wait(timeout=5)
                    collector.join(timeout=10)
                    self.assertFalse(collector.is_alive(), 'Progress collector must finish after quit')
                    self.assertEqual(errors, [])
                    self.assertEqual([r['ratingKey'][0] for r in requests], ['1', '2', '3'])
                    for request in requests:
                        self.assertEqual(request['state'], ['stopped'])
                        self.assertGreater(int(request['time'][0]), 20000)
                        self.assertLessEqual(int(request['time'][0]), 120000)
                    for key, ep in manager.playlist_data.items():
                        request = next(r for r in requests if r['ratingKey'] == [ep['rating_key']])
                        self.assertEqual(int(request['time'][0]), (int(manager.playlist_time[key]) - 2) * 1000)
                    print('\nReal mpv verified (local Plex endpoint): ' + repr([
                        (r['ratingKey'][0], int(r['time'][0]) / 1000) for r in requests]), flush=True)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                    if collector:
                        collector.join(timeout=5)
                    if mpv:
                        mpv.terminate()
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
