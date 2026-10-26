"""Task-local anonymous bootstrap through TikTok JSON APIs using curl_cffi."""
import asyncio
import json

from curl_cffi import requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from loguru import logger

from ..room_state import room_state_flags
from ..request_fingerprint import current_fingerprint
from .direct_models import DirectSession
from .sessions import (PUBLIC_USER_AGENT, normalize_username, validate_room_id,
                       visitor_cookie_header, reject_login_cookie_jar)

ORIGIN = 'https://www.tiktok.com'
_OPTIONS = {'aid': 1988, 'service': 'www.tiktok.com', 'union': False,
            'unionHost': '', 'needFid': False}
_HEADERS = {'content-type': 'application/x-www-form-urlencoded',
            'accept': 'application/json, text/plain, */*', 'origin': ORIGIN,
            'referer': ORIGIN + '/', 'user-agent': PUBLIC_USER_AGENT}


def _response_json(response, body):
    if (response.status_code != 200
            or 'application/json' not in response.headers.get('content-type', '').lower()
            or len(body) > 65536):
        raise ValueError('Invalid visitor JSON response')
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError('Invalid visitor JSON response')
    return payload


async def _request_json(session, method, path, scope, **kwargs):
    body = bytearray()
    def receive(chunk):
        if len(body) + len(chunk) > 65536:
            return CURL_WRITEFUNC_ERROR
        body.extend(chunk)
        return len(chunk)
    response = await getattr(session, method)(ORIGIN + path, timeout=scope.remaining(),
        allow_redirects=False, content_callback=receive, **kwargs)
    scope.remaining()
    reject_login_cookie_jar(session.cookies.jar)
    return _response_json(response, body)


async def _bootstrap(username, room_id, proxy, scope):
    stage = 'check'
    try:
        # No inherited cookie store, browser state or account credentials.
        async with requests.AsyncSession(impersonate=current_fingerprint(), proxy=proxy,
                                          trust_env=False, max_clients=1) as session:
            try:
                reject_login_cookie_jar(session.cookies.jar)
                state = await _request_json(session, 'post', '/ttwid/check/', scope,
                    data=json.dumps(dict(_OPTIONS, fid='', migrate_priority=0), separators=(',', ':')),
                    headers=dict(_HEADERS))
                code = state.get('status_code')
                if type(code) is not int or code < 0:
                    raise ValueError('Invalid visitor check status')
                if code > 1001:
                    stage = 'register'
                    body = dict(_OPTIONS, fid='')
                    if state.get('migrate_info') is not None:
                        if not isinstance(state['migrate_info'], dict):
                            raise ValueError('Invalid visitor migration state')
                        body['migrate_info'] = state['migrate_info']
                    registered = await _request_json(session, 'post', '/ttwid/register/', scope,
                        data=json.dumps(body, separators=(',', ':')),
                        headers=dict(_HEADERS))
                    if type(registered.get('status_code')) is not int or registered['status_code'] != 0:
                        raise ValueError('Visitor registration rejected')
                cookie = visitor_cookie_header(session.cookies.jar)
                if room_id is None:
                    stage = 'room_query'
                    payload = await _request_json(session, 'get', '/api-live/user/room/', scope,
                        params={'aid': 1988, 'uniqueId': username, 'sourceType': 54},
                        headers={'accept': 'application/json', 'user-agent': PUBLIC_USER_AGENT})
                    if ('status_code' in payload and
                            (type(payload['status_code']) is not int or payload['status_code'] != 0)):
                        raise ValueError('Visitor room query rejected')
                    user, live = payload['data']['user'], payload['data'].get('liveRoom') or {}
                    if normalize_username(user['uniqueId']).casefold() != username.casefold():
                        raise ValueError('Visitor room owner mismatch')
                    if room_state_flags(user.get('status'), live.get('status'))[1] is not True:
                        raise ValueError('Visitor room is not confirmed live')
                    room_id = validate_room_id(user['roomId'])
                    cookie = visitor_cookie_header(session.cookies.jar)
                scope.remaining()
                result = DirectSession('delivery_observer', username, room_id,
                                       PUBLIC_USER_AGENT, cookie, proxy)
                logger.info('匿名访客接口初始化完成 | transport=curl_cffi | role=delivery_observer '
                            '| bootstrap=direct_wss | classification=visitor_api_ready')
                return result
            finally:
                session.cookies.jar.clear()
    except TimeoutError:
        raise
    except Exception as exc:
        logger.warning('匿名访客接口初始化失败 | stage={} | error_type={} '
                       '| classification=visitor_api_failed', stage, type(exc).__name__)
        raise ValueError('Anonymous visitor API initialization failed') from None


def bootstrap_visitor(username, room_id, proxy, scope):
    async def run():
        task = asyncio.create_task(_bootstrap(username, room_id, proxy, scope))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=min(.05, scope.remaining()))
            scope.remaining()
            return await task
        finally:
            if not task.done():
                task.cancel()
            # Cancel/remove libcurl's handle on its owner event loop, never close
            # a synchronous Curl handle from the deadline timer's thread.
            await asyncio.gather(task, return_exceptions=True)
    return asyncio.run(run())
