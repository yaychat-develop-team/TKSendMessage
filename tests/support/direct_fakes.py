from tiktok_live.wss.direct_models import DirectSession
from tiktok_live.heartbeat_socket import FrameEvidence


def visitor():
    return DirectSession('delivery_observer', 'host', '42', 'UA', 'ttwid=SYNTHETIC')


class WaitingConnection:
    def __init__(self, session, *, on_comment, on_connected, connect_timeout):
        self.session = session
        self.room_id = session.room_id
        self.connected = False
        self.evidence = FrameEvidence()
        self.on_connected = on_connected
        self.closed = False
        self.stop = None

    def run(self, stop):
        self.stop = stop
        self.connected = True
        self.on_connected()
        stop.wait(5)
        self.close()

    def close(self):
        self.connected = False
        self.closed = True
        if self.stop is not None:
            self.stop.set()
