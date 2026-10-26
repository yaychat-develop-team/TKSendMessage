"""Offline verification of actual Python code frozen in a candidate executable."""
import argparse
import hashlib
import importlib.abc
import importlib.util
import json
import marshal
import os
from pathlib import Path
import sys
import tempfile
from types import CodeType
import unittest
from PyInstaller.archive.readers import CArchiveReader

MODULES = ('tiktok_live', 'tiktok_live.wss', 'tiktok_live.wss.direct_models',
           'tiktok_live.wss.direct_signer', 'tiktok_live.wss.protocol',
           'tiktok_live.wss.sessions', 'tiktok_live.wss.isolated_socket',
           'tiktok_live.wss.startup',
           'tiktok_live.wss.connection', 'tiktok_live.heartbeat_socket',
           'tiktok_live.heartbeat', 'listen_msg', 'tiktok_live.browser_auth',
           'tiktok_live.realtime_delivery')


def canonical(code):
    return code.replace(co_filename='<verified>', co_consts=tuple(
        canonical(value) if isinstance(value, CodeType) else value for value in code.co_consts))


def verify_inventory(candidate, source):
    archive = CArchiveReader(str(candidate))
    pyz = archive.open_embedded_archive('PYZ.pyz')
    names = list(archive.toc) + list(pyz.toc)
    forbidden = ('tiktoklive', 'tiktokliveproto', 'node_modules', 'node.exe', 'execjs', 'py_mini_racer', 'iv8')
    if any(any(token in name.lower() for token in forbidden) for name in names):
        raise ValueError('Forbidden runtime in candidate')
    if any('/runtime/' in '/' + name.replace('\\', '/').lower() or 'tiktok_accounts' in name.lower()
           or 'js_reverse_cache' in name.lower() for name in names):
        raise ValueError('Private/development input in candidate')
    hashes = {}
    transport_root = Path(importlib.util.find_spec('websocket').submodule_search_locations[0])
    transport_modules = tuple(name for name in pyz.toc if name == 'websocket' or name.startswith('websocket.'))
    if not transport_modules:
        raise ValueError('Frozen transport missing')
    if 'socks' not in pyz.toc:
        raise ValueError('Frozen SOCKS5 transport missing')
    transport_modules += ('socks',)
    application_modules = tuple(name for name in pyz.toc if name in ('settings', 'listen_msg', 'send_live_message', 'desktop')
                                or name.startswith(('tiktok_live.', 'desktop.')))
    modules = tuple(dict.fromkeys(MODULES + application_modules + transport_modules))
    for name in modules:
        path = (transport_root.joinpath(*name.split('.')[1:]) if name.startswith('websocket')
                else source.joinpath(*name.split('.')))
        if name == 'socks':
            path = Path(importlib.util.find_spec('socks').origin)
        path = path / '__init__.py' if path.is_dir() else path.with_suffix('.py')
        expected = compile(path.read_bytes(), str(path), 'exec', dont_inherit=True, optimize=0)
        actual = pyz.extract(name)
        if canonical(actual) != canonical(expected):
            raise ValueError('Frozen/source mismatch: ' + name)
        hashes[name] = hashlib.sha256(marshal.dumps(canonical(actual))).hexdigest()
    entry = marshal.loads(archive.extract('app'))
    expected_entry = compile((source / 'app.py').read_bytes(), 'app.py', 'exec', dont_inherit=True, optimize=0)
    if canonical(entry) != canonical(expected_entry):
        raise ValueError('Frozen/source mismatch: app entry point')
    hashes['app'] = hashlib.sha256(marshal.dumps(canonical(entry))).hexdigest()
    return pyz, hashes, len(names)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--settings-regression', action='store_true')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    scratch = project / 'build/work/direct-frozen-temp'
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    private = [str((project / p).resolve()).lower() + os.sep for p in ('runtime', 'dist/runtime')]
    def offline(event, values):
        if event in ('socket.connect', 'socket.sendto'):
            target = values[-1]
            if not isinstance(target, tuple) or target[0] not in ('127.0.0.1', '::1'):
                raise PermissionError('Remote network denied')
        if event == 'socket.getaddrinfo' and values[0] not in ('localhost', '127.0.0.1', '::1'):
            raise PermissionError('Remote DNS denied')
        if event == 'open' and isinstance(values[0], (str, bytes, os.PathLike)):
            path = os.path.abspath(os.fsdecode(values[0])).lower()
            if any(path.startswith(value) for value in private):
                raise PermissionError('Real runtime denied')
    sys.addaudithook(offline)
    pyz, hashes, count = verify_inventory(args.candidate, args.source)
    frozen_entry = marshal.loads(CArchiveReader(str(args.candidate)).extract('app'))
    class FrozenLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'app':
                return importlib.util.spec_from_loader(fullname, self, is_package=False)
            if fullname in pyz.toc and (fullname == 'listen_msg' or fullname.startswith('tiktok_live')
                                       or fullname == 'settings' or fullname == 'desktop' or fullname.startswith('desktop.')
                                       or fullname == 'socks'
                                       or fullname == 'websocket' or fullname.startswith('websocket.')):
                return importlib.util.spec_from_loader(fullname, self, is_package=pyz.toc[fullname][0] == 1)
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            # Execute archive bytecode, using validated staged resource paths
            # for browser_env.json etc. No product source is imported/executed.
            path = args.source.joinpath(*module.__name__.split('.'))
            module.__file__ = str(path/'__init__.py' if path.is_dir() else path.with_suffix('.py'))
            exec(frozen_entry if module.__name__ == 'app' else pyz.extract(module.__name__), module.__dict__)
    sys.meta_path.insert(0, FrozenLoader())
    names = [
        'tests.test_isolated_wss_socket', 'tests.test_direct_connection',
        'tests.test_direct_protocol', 'tests.test_startup_scope']
    if args.settings_regression:
        names += ['tests.test_room_restriction', 'tests.test_parallel_runner']
        names += ['tests.test_consecutive_unobserved', 'tests.test_request_fingerprint']
        names += ['tests.test_long_run_recovery']
        names += ['tests.test_observer_proxy']
        names += ['tests.test_desktop_settings', 'tests.test_desktop_workers']
        names += ['tests.test_single_instance', 'tests.test_startup_safety',
                  'tests.test_main_window.MainWindowTests.test_cancelling_account_deletion_preserves_file_and_selection',
                  'tests.test_main_window.MainWindowTests.test_selected_account_can_be_deleted_without_exposing_full_id']
        names += ['tests.test_main_window.MainWindowTests.test_room_execution_setting_labels_limit_persists_and_is_readonly_while_running',
                  'tests.test_main_window.MainWindowTests.test_open_delivery_details_tracks_all_rooms_without_cross_account_or_reason_leaks']
        names += ['tests.test_realtime_delivery']
        names += ['tests.test_initialized_connection', 'tests.test_initial_fetch_request']
        names += ['tests.test_legacy_entrypoint', 'tests.test_regression',
                  'tests.test_room_enter_before_chat', 'tests.test_listener_shutdown']
        names += ['tests.test_main_window.MainWindowTests.test_blank_logs_do_not_desynchronize_visible_retention',
                  'tests.test_main_window.MainWindowTests.test_log_follow_can_pause_resume_and_visible_cache_evicts_old_lines',
                  'tests.test_parallel_runner.ParallelRunnerTests.test_failure_reasons_only_forward_known_codes_and_are_bounded']
        names += ['tests.test_main_window.MainWindowTests.test_delivery_distinguishes_failure_uncertainty_and_cancel_and_opens_details',
                  'tests.test_main_window.MainWindowTests.test_log_filters_search_and_clear_do_not_restore_removed_records',
                  'tests.test_main_window.MainWindowTests.test_log_filter_retention_is_bounded_and_secrets_are_not_cached']
        names += ['tests.test_main_window.MainWindowTests.test_busy_allows_readonly_details_and_settings_and_restores_editing',
                  'tests.test_main_window.MainWindowTests.test_delivery_icons_supply_native_pixels_at_fractional_and_high_dpi',
                  'tests.test_main_window.MainWindowTests.test_brand_icon_preserves_high_dpi_pixels_without_changing_layout_size',
                  'tests.test_main_window.MainWindowTests.test_delivery_matches_legacy_hex_account_ids',
                  'tests.test_main_window.MainWindowTests.test_delivery_column_aggregates_observation_not_http_success_and_survives_refresh',
                  'tests.test_main_window.MainWindowTests.test_delivery_multiple_rooms_is_per_account_and_cancel_keeps_partial_results']
        names += ['tests.test_public_room_page', 'tests.test_room_query_retries', 'tests.test_account_room_page']
        names += ['tests.test_visitor_api_bootstrap', 'tests.test_pure_api_room', 'tests.test_room_lookup_stability', 'tests.test_room_api_fallback', 'tests.test_fresh_ticket_guard', 'tests.test_chat_csrf_flow',
                  'tests.test_saved_request_identity.SavedRequestIdentityTests.test_post_rejects_saved_ticket_headers_without_signing_state']
        names += ['tests.test_runtime_settings_effective','tests.test_send_heartbeat','tests.test_message_burst',
                  'tests.test_heartbeat_interval', 'tests.test_direct_sessions',
                  'tests.test_heartbeat_logging',
                  'tests.test_parallel_logs.ParallelLogTests.test_only_observed_message_uses_success_level',
                  'tests.test_main_window.MainWindowTests.test_observed_delivery_is_green_but_unobserved_acceptance_is_not',
                  'tests.test_observer_room_lookup',
                  'tests.test_message_listener.RunnerObservationTests.test_observer_uses_account_room_before_opening_guest_connection',
                  'tests.test_message_listener.RunnerObservationTests.test_room_lookup_uses_only_selected_account_and_closes_session',
                  'tests.test_message_listener.RunnerObservationTests.test_room_lookup_rejects_an_explicitly_different_owner',
                  'tests.test_main_window.MainWindowTests.test_results_stay_visible_without_scrolling_upper_panel',
                  'tests.test_main_window.MainWindowTests.test_default_account_area_shows_four_rows_without_clipping',
                  'tests.test_main_window.MainWindowTests.test_compact_message_leaves_large_log_after_account_refresh_and_resize',
                  'tests.test_main_window.MainWindowTests.test_splitter_can_adjust_log_without_shrinking_below_readable_budget',
                  'tests.test_main_window.MainWindowTests.test_native_font_keeps_stat_values_below_titles_and_inside_normal_window',
                  'tests.test_main_window.MainWindowTests.test_small_window_does_not_hide_log_behind_upper_panel']
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = dict(archive_inventory=True, loopback_protocol=result.wasSuccessful(),
                  tests=result.testsRun, inventory_entries=count, module_hashes=hashes,
                  sha256=hashlib.sha256(args.candidate.read_bytes()).hexdigest())
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
