"""Environment-state producer migration, not yet a complete initializer.

DOM, location, descriptor and native-source probes are migrated and
independently characterized, plus a subset of interaction-state derivation.
Raw facts are observations, never a caller-supplied envcode/SDK flag mask.
Other flag producers/lifecycle must be migrated before production use.
"""
from collections.abc import Mapping
import math
import re

from .frontier import FrontierError


def dom_environment_is_suspicious(user_agent: str, facts: Mapping[str, object]) -> bool:
    """Port legacy entry 40695, including its IE-history short circuit.

    Input values correspond to JS typeof, Object.prototype.toString.call and
    ordinary toString results from a session-owned environment. This function
    does not launch a browser, call SDK code, inspect cookies, or infer facts.
    """
    if not isinstance(user_agent, str) or len(user_agent) > 2048:
        raise FrontierError('Invalid environment facts')
    if not isinstance(facts, Mapping) or len(facts) > 128:
        raise FrontierError('Invalid environment facts')
    kinds = ('window_type', 'document_type', 'navigator_type', 'location_type', 'history_type')
    tags = ('window_to_string', 'window_object_tag', 'document_to_string',
            'navigator_to_string', 'history_to_string')
    for key in kinds:
        if facts.get(key) not in ('undefined', 'object', 'function'):
            raise FrontierError('Invalid environment facts')
    for key in tags:
        value = facts.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 4096):
            raise FrontierError('Invalid environment facts')
        # A missing object is representable, but a present object's successful
        # toString observation cannot be silently omitted.
        owner = key.split('_', 1)[0] + '_type'
        if facts.get(owner) != 'undefined' and value is None:
            raise FrontierError('Missing environment facts')
    if 'document_mode' not in facts:
        raise FrontierError('Missing environment facts')
    document_mode = facts['document_mode']
    if document_mode is not None and (type(document_mode) is not int or not 0 <= document_mode <= 100):
        raise FrontierError('Invalid environment facts')
    lowered = user_agent.lower()
    if 'electron' in lowered:
        return False
    if (facts['window_type'] == 'undefined'
            or facts['window_to_string'] != '[object Window]'
            or facts['window_object_tag'] != '[object Window]'
            or facts['document_type'] == 'undefined'
            or 'Document' not in facts['document_to_string']
            or facts['navigator_type'] == 'undefined'
            or facts['navigator_to_string'] != '[object Navigator]'
            or 'jsdom' in user_agent
            or facts['location_type'] == 'undefined'):
        return True
    # Upstream entry 1514 lowercases UA but searches uppercase 'MSIE'; retain
    # that behavior rather than silently correcting the vendor's condition.
    ie_environment = 'MSIE' in lowered or bool(document_mode)
    if ie_environment:
        return False
    return (facts['history_type'] == 'undefined'
            or facts['history_to_string'] != '[object History]')


def interaction_code(events: list[Mapping[str, object]]) -> int:
    """Port event-presence/rate flags of entries 34598 and 37144.

    This covers document mouse/click/key observations and their untrusted
    flag. It does NOT yet model the separate private isTrusted provenance
    state (kFakeOperations), lifecycle enabling or SDK sample retention.
    Consequently it is not installed as a complete initialization fallback.
    """
    if type(events) is not list or len(events) > 4096:
        raise FrontierError('Invalid interaction observations')
    samples = {'mousemove': [], 'click': [], 'keydown': []}
    untrusted = False
    for event in events:
        if not isinstance(event, Mapping) or len(event) > 16:
            raise FrontierError('Invalid interaction observations')
        kind = event.get('kind')
        if not isinstance(kind, str) or kind not in samples or type(event.get('trusted')) is not bool:
            raise FrontierError('Invalid interaction observations')
        for key in ('x', 'y', 'd'):
            value = event.get(key)
            if type(value) not in (int, float) or abs(value) > 2**53 or not math.isfinite(value):
                raise FrontierError('Invalid interaction observations')
        samples[kind].append((float(event['x']), float(event['y']), float(event['d'])))
        untrusted |= not event['trusted']

    def mean_rate(values, threshold, keyboard=False):
        # Upstream requires > threshold samples, skips zero time differences,
        # and uses geometric movement per millisecond / reciprocal key delay.
        if len(values) <= threshold:
            return 0.0
        rates = []
        for left, right in zip(values, values[1:]):
            elapsed = right[2] - left[2]
            if elapsed:
                dx, dy = right[0] - left[0], right[1] - left[1]
                rates.append(1 / elapsed if keyboard else math.sqrt(dx * dx + dy * dy) / elapsed)
        return sum(rates) / len(rates) if rates else 0.0

    mouse = samples['mousemove']
    clicks = samples['click']
    keys = samples['keydown']
    code = 128 if untrusted else 0
    if not mouse:
        code |= 2
    elif mean_rate(mouse, 2) > 50:
        code |= 16
    if not clicks:
        code |= 4
    if not keys:
        code |= 8
    elif mean_rate(keys, 5, keyboard=True) > 0.5:
        code |= 32
    return code


def location_is_suspicious(user_agent: str, href: str) -> bool:
    """Legacy entry 47632. Fingerprint probe, NOT a destination allowlist."""
    if not isinstance(user_agent, str) or len(user_agent) > 2048 or not isinstance(href, str) or len(href) > 4096:
        raise FrontierError('Invalid environment facts')
    if 'electron' in user_agent.lower() or not href:
        return False
    return (href.startswith('file') or href.startswith('http://localhost')
            or re.match(r'^https?://([0-9]{1,3}(\.[0-9]{1,3}){3}|[a-f0-9]{1,4}(:[a-f0-9]{1,4}){7})', href) is not None)


def undetected_driver_probe(touch_getter_type: str, date_probe_stack: str,
                           rtt_descriptor_type: str) -> bool:
    """Port entries 21228/11835 from primitive descriptor/error observations.

    `missing` represents the absent maxTouchPoints descriptor/get access error;
    `error` represents an exception getting the connection rtt descriptor.
    No private SDK booleans or final env-code bytes are accepted as inputs.
    """
    kinds = ('undefined', 'function', 'object', 'string', 'number', 'boolean', 'symbol', 'bigint')
    if (not isinstance(touch_getter_type, str) or touch_getter_type not in kinds + ('missing', 'error')
            or not isinstance(rtt_descriptor_type, str) or rtt_descriptor_type not in kinds + ('error',)
            or not isinstance(date_probe_stack, str) or len(date_probe_stack) > 4096):
        raise FrontierError('Invalid environment facts')
    touch = 1 if touch_getter_type in ('missing', 'error') else 2 if touch_getter_type == 'function' else 3
    date = 2 if 'Proxy.' in date_probe_stack or 'proxy.' in date_probe_stack else 1
    rtt = 3 if rtt_descriptor_type == 'error' else 2 if rtt_descriptor_type == 'object' else 1
    return 2 in (touch, date, rtt)


def native_hook_is_suspicious(function_sources: Mapping[str, str | None],
                              plugins_to_string: str, *, document_mode: int | None = None) -> bool:
    """Entry 46949; compare primitive function-source/plugin observations.

    None means observing a particular method's toString threw, which the SDK
    skips. Missing observations are not equivalent to an observed exception.
    """
    names = ('canvas.toDataURL', 'navigator.toString', 'document.createElement',
             'document.getElementById', 'JSON.stringify', 'JSON.parse',
             'window.setInterval', 'window.eval', 'window.matchMedia')
    if (not isinstance(function_sources, Mapping) or len(function_sources) > 32
            or not isinstance(plugins_to_string, str) or len(plugins_to_string) > 4096
            or document_mode is not None and (type(document_mode) is not int or not 0 <= document_mode <= 100)):
        raise FrontierError('Invalid environment facts')
    for name in names:
        if name not in function_sources:
            raise FrontierError('Missing environment facts')
        source = function_sources[name]
        if source is not None and (not isinstance(source, str) or len(source) > 4096):
            raise FrontierError('Invalid environment facts')
    # ECMAScript RegExp \s: explicit set avoids Python-only whitespace such as
    # U+0085 silently changing the SDK's source-text check.
    whitespace = '\u0009\u000b\u000c\u0020\u00a0\ufeff\u000a\u000d\u2028\u2029\u1680\u202f\u205f\u3000' + ''.join(chr(code) for code in range(0x2000, 0x200B))
    if not document_mode:
        for name in names:
            source = function_sources[name]
            if source is not None and 'nativecode' not in source.translate(dict.fromkeys(map(ord, whitespace))):
                return True
    return plugins_to_string != '[object PluginArray]'
