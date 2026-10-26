"""Non-consuming, task-local receipt evidence while the POST burst is running."""
from collections import OrderedDict
from contextvars import copy_context
from itertools import chain
import threading
import time
from listen_msg import _normalize_comment, _clean_username


class RealtimeDelivery:
    def __init__(self, listener, *, account, message, lock, notify, log_observed,
                 timeout=None, log_timeout=None, should_cancel=None, on_outcome=None):
        self.listener = listener
        self.account = account
        self.message = message
        self.lock = lock
        self.notify = notify
        self.log_observed = log_observed
        self.context = copy_context()
        self.sent = set()
        self.observed = {}
        self.logged = set()
        self.claimed = {}
        self.pending = {}
        self.sealed = set()
        self.recent = OrderedDict()
        self.closed = False
        self.unsubscribe = None
        self.restriction = None
        self.timeout = timeout
        self.log_timeout = log_timeout
        self.should_cancel = should_cancel
        self.on_outcome = on_outcome
        self.deadlines = {}
        self.expired = set()
        self.timeout_logged = set()
        self.condition = threading.Condition(lock)
        self.monitor = None

    def __enter__(self):
        subscribe = getattr(self.listener, 'subscribe_comments', None)
        if callable(subscribe):
            self.unsubscribe = subscribe(self.receive)
            if self.timeout is not None and self.log_timeout is not None:
                self.monitor = threading.Thread(target=self._monitor, name='delivery-deadline', daemon=True)
                self.monitor.start()
        return self

    def __exit__(self, *exc):
        with self.lock:
            self.closed = True
            self.condition.notify_all()
        # Do not hold the progress lock while waiting for an in-flight callback.
        if self.unsubscribe is not None:
            self.unsubscribe()
        if self.monitor is not None:
            self.monitor.join(1)

    def _monitor(self):
        with self.condition:
            while not self.closed:
                waiting = [(deadline,index) for index,deadline in self.deadlines.items()
                    if index not in self.observed and index not in self.sealed and index not in self.expired]
                if not waiting:
                    self.condition.wait()
                    continue
                deadline,index = min(waiting)
                remaining = deadline-time.monotonic()
                if remaining > 0:
                    self.condition.wait(remaining)
                    continue
                self.expired.add(index)
                if self.should_cancel is not None and self.should_cancel():
                    continue
                if self._buffered(index) is not None:
                    continue
                if not getattr(self.listener,'available',True) or getattr(self.listener,'error',None) is not None:
                    self._outcome(index, 'other')
                    continue
                coverage = getattr(self.listener, 'observation_available_since', None)
                if callable(coverage) and not coverage(self.pending[index][1]):
                    self._outcome(index, 'other')
                    continue
                self.timeout_logged.add(index)
                self.context.copy().run(self.log_timeout,index,self.pending[index][0])
                self._outcome(index, 'not_observed')
                self.notify()

    def _outcome(self, index, status):
        if self.on_outcome is not None:
            self.context.copy().run(self.on_outcome, index, status)

    def remaining(self, index, default):
        with self.lock:
            if self.monitor is None or index not in self.deadlines:
                return default
            return max(0.0,self.deadlines[index]-time.monotonic())

    def timeout_reported(self, index):
        with self.lock:
            return index in self.timeout_logged

    def _buffered(self, index):
        if index in self.sealed or index not in self.pending:
            return None
        snapshot = getattr(self.listener,'snapshot_comments',None)
        events = chain(tuple(self.recent.values()), snapshot() if callable(snapshot) else ())
        for event in events:
            if self._match(event,self.pending[index]) and self.claim(index,event):
                self._observe(index,event)
                return self.observed.get(index)
        return None

    def _match(self, event, identity):
        message_id, since, names, user_ids = identity
        if (message_id and event.message_id != message_id) or event.observed_at < since:
            return False
        if not message_id and not (names or user_ids):
            return False
        if _normalize_comment(event.comment) != _normalize_comment(self.message):
            return False
        observed_names = {_clean_username(event.unique_id), _clean_username(event.nickname)} - {''}
        return not (names or user_ids) or bool(names & observed_names or event.user_id in user_ids)

    def _observe(self, index, event):
        if index in self.observed or index in self.sealed:
            return
        if not self.claim(index, event):
            return
        self.observed[index] = event
        self.logged.add(index)
        # Listener callbacks run on the WSS thread: retain the sender/task context.
        self.context.copy().run(self.log_observed, index, event.message_id)
        self._outcome(index, 'observed')
        self.condition.notify_all()
        self.notify()

    def receive(self, event):
        with self.lock:
            if self.closed or not event.message_id:
                return
            self.recent.setdefault(event.message_id, event)
            while len(self.recent) > 4096:
                self.recent.popitem(last=False)
            for index, identity in sorted(self.pending.items(),key=lambda item:not bool(item[1][0])):
                if self._match(event, identity):
                    self._observe(index, event)

    def register(self, index, result, since, user_ids):
        with self.lock:
            if self.closed:
                return
            if isinstance(result, dict):
                self.sent.add(index)
                if result.get('roomCommentsRestricted'):
                    self.restriction = 'room_comment_restricted'
                elif result.get('accountBanned'):
                    self.restriction = 'account_banned'
                elif result.get('accountSilenced'):
                    self.restriction = 'account_silenced'
            if isinstance(result, dict) and (result.get('ok') or result.get('requiresObservation')):
                response = result.get('response')
                data = response.get('data') if isinstance(response, dict) else None
                message_id = str(data.get('msg_id_str') or data.get('msg_id') or '') if isinstance(data, dict) else ''
                names = {_clean_username(self.account.get('username')),
                         _clean_username(result.get('accountUsername'))} - {''}
                self.pending[index] = (message_id, since, names, set(user_ids))
                self.sealed.discard(index)
                self.expired.discard(index)
                self.timeout_logged.discard(index)
                if self.monitor is not None:
                    self.deadlines[index] = time.monotonic()+max(0.0,float(self.timeout))
                self._buffered(index)
                self.condition.notify_all()
            else:
                self._outcome(index, 'other')
            self.notify()

    def get(self, index):
        with self.lock:
            return self.observed.get(index)

    def claim(self, index, event):
        with self.lock:
            message_id = getattr(event, 'message_id', '')
            if not message_id:
                return True
            if self.claimed.get(message_id, index) != index:
                return False
            self.claimed[message_id] = index
            return True

    def resolve(self, index, event):
        """Atomically reconcile a wait result and seal a missed/failed receipt."""
        with self.lock:
            observed = self.observed.get(index)
            if observed is None:
                observed = self._buffered(index)
            if observed is not None:
                return observed
            if event is not None and self.claim(index, event):
                if getattr(event, 'message_id', ''):
                    self._observe(index, event)
                return event
            self.sealed.add(index)
            self.condition.notify_all()
            return None

    def finalize_display(self, index, status):
        """Return False if the real-time path already printed the success."""
        with self.lock:
            self._outcome(index, status)
            self.sealed.add(index)
            self.condition.notify_all()
            if status == 'not_observed' and index in self.timeout_logged:
                return False
            if status == 'observed':
                if index in self.logged:
                    return False
                self.logged.add(index)
            return True
