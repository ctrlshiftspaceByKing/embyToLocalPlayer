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


class PlexState:
    """Model the real server: timeline is acknowledged but ignored for later items."""
    def __init__(self):
        self.items = {}
        self.drop_updates = {}
        self.drop_scrobbles = {}
        self.ignored_items = set()
        self.wrong_metadata_key = False

    def request(self, url, params=None, **kwargs):
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        params = params or {}
        if path == '/:/timeline':
            return ''  # Reproduce the observed HTTP 200 without a saved playback record.
        if path in ('/:/progress', '/:/scrobble', '/:/unscrobble'):
            key = str(params['key'])
            assert params.get('identifier') == 'com.plexapp.plugins.library'
            if path == '/:/progress':
                self.ignored_items.discard(key)
                remaining = self.drop_updates.get(key, 0)
                if remaining:
                    self.drop_updates[key] = remaining - 1
                    self.ignored_items.add(key)
                    return ''
            if key in self.ignored_items:
                return ''
            item = self.items.setdefault(key, {'ratingKey': key, 'viewOffset': 0,
                                              'viewCount': 0, 'duration': 120000})
            if path == '/:/progress':
                item['viewOffset'] = int(params['time'])
            elif path == '/:/scrobble':
                remaining = self.drop_scrobbles.get(key, 0)
                if remaining:
                    self.drop_scrobbles[key] = remaining - 1
                    return ''
                item['viewCount'] += 1
                item['viewOffset'] = 0
            else:
                item['viewCount'] = 0
        elif path.startswith('/library/metadata/'):
            key = path.rsplit('/', 1)[-1]
            item = self.items.get(key, {'ratingKey': key, 'viewOffset': 0, 'viewCount': 0, 'duration': 120000})
            if self.wrong_metadata_key:
                item = dict(item, ratingKey='wrong-episode')
            return {'MediaContainer': {'Metadata': [dict(item)]}}
        else:
            raise AssertionError('Unexpected Plex endpoint: ' + path)
        return ''


def make_environment():
    raw = configparser.ConfigParser(interpolation=None)
    raw.read_dict({'dev': {}, 'emby': {'update_progress': 'yes'}, 'playlist': {}})
    configs = types.SimpleNamespace(raw=raw, ini_str_split=lambda *a, **kw: [], script_proxy='')
    plex_state = PlexState()
    ns = dict(configs=configs, logger=Mock(), queue=queue, threading=threading, time=time,
              os=os, json=json, urllib=urllib, ssl_context=None, MPV=object, MPVError=PropertyError,
              EmbyApiThin=lambda data: None, sync_third_party_for_eps=Mock(),
              prefetch_data={'on': True, 'stop_sec_dict': {}}, plex_state=plex_state,
              requests_urllib=Mock(side_effect=plex_state.request))
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
        collector_thread = threading.current_thread()
        self.ns['time'] = types.SimpleNamespace(
            sleep=lambda seconds: mpv.sleep(seconds) if threading.current_thread() is collector_thread else None)
        self.manager.player_kwargs = {'mpv': mpv}
        self.manager.update_playlist_time_loop()

    def requests(self):
        return [dict(call.kwargs['params'], ratingKey=str(call.kwargs['params']['key']))
                for call in self.ns['requests_urllib'].call_args_list
                if call.args[0].endswith('/:/progress')]

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

    def test_plex_request_identifies_each_library_item(self):
        self.run_frames([('S01E01', 119), ('S01E02', 118), ('S01E03', 45)])
        self.manager.update_playback_for_eps()
        for params in self.requests():
            self.assertEqual(params.get('key'), params['ratingKey'])
            self.assertEqual(params.get('identifier'), 'com.plexapp.plugins.library')

    def test_five_episode_persisted_state_including_all_middle_episodes(self):
        self.manager = make_manager(self.ns, count=5)
        self.run_frames([(f'S01E{i:02}', 119 if i < 5 else 45) for i in range(1, 6)])
        self.assertEqual(set(self.ns['plex_state'].items), {'1', '2', '3', '4'})
        self.manager.update_playback_for_eps()
        items = self.ns['plex_state'].items
        self.assertEqual(set(items), {'1', '2', '3', '4', '5'})
        self.assertTrue(all(items[str(i)]['viewCount'] == 1 for i in range(1, 5)))
        self.assertEqual(items['5']['viewCount'], 0)
        self.assertEqual(items['5']['viewOffset'], 43000)

    def test_http_200_with_ignored_middle_episode_is_retried(self):
        self.manager = make_manager(self.ns, count=5)
        self.ns['plex_state'].drop_updates['3'] = 1
        self.run_frames([(f'S01E{i:02}', 119 if i < 5 else 45) for i in range(1, 6)])
        self.manager.update_playback_for_eps()
        self.assertEqual(set(self.ns['plex_state'].items), {'1', '2', '3', '4', '5'})
        self.assertEqual([r['ratingKey'] for r in self.requests()].count('3'), 2)

    def test_unsaved_progress_is_not_deduplicated_and_retries_on_close(self):
        self.ns['plex_state'].drop_updates['1'] = 2
        self.run_frames([('S01E01', 119), ('S01E02', 50)])
        self.assertNotIn('S01E01', self.manager._submitted_playlist_time)
        self.manager.update_playback_for_eps()
        self.assertEqual(set(self.ns['plex_state'].items), {'1', '2'})

    def test_wrong_episode_readback_cannot_confirm_submission(self):
        self.ns['plex_state'].wrong_metadata_key = True
        self.run_frames([('S01E01', 119), ('S01E02', 50)])
        self.manager.update_playback_for_eps()
        self.assertEqual(self.manager._submitted_playlist_time, {})

    def test_incomplete_episode_updates_resume_without_marking_watched(self):
        self.run_frames([('S01E01', 60)])
        self.manager.update_playback_for_eps()
        self.assertEqual(self.ns['plex_state'].items['1']['viewOffset'], 58000)
        self.assertEqual(self.ns['plex_state'].items['1']['viewCount'], 0)
        self.assertFalse(any(call.args[0].endswith('/:/scrobble')
                             for call in self.ns['requests_urllib'].call_args_list))

    def test_saved_end_position_without_watched_flag_is_not_success(self):
        self.ns['plex_state'].drop_scrobbles['1'] = 2
        self.run_frames([('S01E01', 119), ('S01E02', 50)])
        self.assertNotIn('S01E01', self.manager._submitted_playlist_time)
        self.assertEqual(self.ns['plex_state'].items['1']['viewCount'], 0)
        self.manager.update_playback_for_eps()
        self.assertEqual(self.ns['plex_state'].items['1']['viewCount'], 1)

    def test_repeated_completed_submission_does_not_increment_watch_count(self):
        ep = self.manager.playlist_data['S01E01']
        self.ns['change_plex_play_position'](stop_sec=117, **ep)
        self.ns['change_plex_play_position'](stop_sec=117, **ep)
        self.assertEqual(self.ns['plex_state'].items['1']['viewCount'], 1)
        self.assertEqual(len(self.requests()), 1)

    def test_unknown_duration_does_not_mark_watched(self):
        self.ns['plex_state'].items['1'] = {'ratingKey': '1', 'viewCount': 0, 'viewOffset': 0, 'duration': 0}
        ep = dict(self.manager.playlist_data['S01E01'], duration=0)
        self.ns['change_plex_play_position'](stop_sec=117, **ep)
        self.assertEqual(self.ns['plex_state'].items['1']['viewCount'], 0)
        self.assertEqual(self.ns['plex_state'].items['1']['viewOffset'], 117000)


@unittest.skipUnless(os.environ.get('ETLP_TEST_REAL_MPV') == '1' and shutil.which('mpv') and os.name != 'nt',
                     'set ETLP_TEST_REAL_MPV=1 to run real mpv integration')
class RealMpvProgressTests(unittest.TestCase):
    def test_five_episodes_persist_all_middle_episodes_then_quit(self):
        ns = make_environment()
        load_definitions('utils/net_tools.py', {'safe_url', 'requests_urllib'}, ns)
        spec = importlib.util.spec_from_file_location('test_mpv_jsonipc', ROOT / 'utils/python_mpv_jsonipc.py')
        ipc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ipc)
        ns['MPVError'] = ipc.MPVError
        manager = make_manager(ns, count=5)
        requests = []
        plex_state = PlexState()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                params = {key: values[0] for key, values in urllib.parse.parse_qs(parts.query).items()}
                if urllib.parse.unquote(parts.path) == '/:/progress':
                    requests.append(urllib.parse.parse_qs(parts.query))
                body = plex_state.request('http://localhost' + parts.path, params=params)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(body or {}).encode())

            def log_message(self, *args):
                pass

        def wait_until(predicate, timeout=80):
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
                    '--speed=8', '--audio-pitch-correction=no', '--force-media-title=S01E01',
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
                    wait_until(lambda: len(mpv.command('get_property', 'playlist')) == 5)
                    wait_until(lambda: all(plex_state.items.get(str(i), {}).get('viewCount', 0) > 0
                                           for i in range(1, 5)))
                    self.assertIsNone(process.poll(), 'All four earlier episodes must report before mpv exits')
                    self.assertEqual([r['key'][0] for r in requests], ['1', '2', '3', '4'])
                    self.assertEqual(set(plex_state.items), {'1', '2', '3', '4'})
                    self.assertTrue(all(plex_state.items[str(i)]['viewCount'] == 1 for i in range(1, 5)))
                    wait_until(lambda: mpv.command('get_property', 'media-title') == 'S01E05'
                               and (mpv.command('get_property', 'time-pos') or 0) >= 45)
                    time.sleep(0.6)  # Allow the production sampler to retain the current position.
                    self.assertEqual(len(requests), 4, 'Current episode is flushed on quit')
                    mpv.command('quit')
                    process.wait(timeout=5)
                    collector.join(timeout=10)
                    self.assertFalse(collector.is_alive(), 'Progress collector must finish after quit')
                    self.assertEqual(errors, [])
                    self.assertEqual([r['key'][0] for r in requests], ['1', '2', '3', '4', '5'])
                    self.assertEqual(set(plex_state.items), {'1', '2', '3', '4', '5'})
                    self.assertEqual(plex_state.items['5']['viewCount'], 0)
                    self.assertEqual(plex_state.items['5']['viewOffset'], int(requests[-1]['time'][0]))
                    for request in requests:
                        self.assertEqual(request['identifier'], ['com.plexapp.plugins.library'])
                        self.assertGreater(int(request['time'][0]), 20000)
                        self.assertLessEqual(int(request['time'][0]), 120000)
                    for key, ep in manager.playlist_data.items():
                        request = next(r for r in requests if r['key'] == [ep['rating_key']])
                        self.assertEqual(int(request['time'][0]), (int(manager.playlist_time[key]) - 2) * 1000)
                    print('\nReal mpv verified (stateful local Plex endpoint): ' + repr([
                        (key, item['viewCount'], item['viewOffset'])
                        for key, item in plex_state.items.items()]), flush=True)
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
