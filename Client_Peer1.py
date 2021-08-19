import random
import ssl
import websockets
import asyncio
import os
import sys
import json
import argparse

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

PIPELINE_DESC = '''
webrtcbin name=sendrecv bundle-policy=max-bundle stun-server=stun://stun.l.google.com:19302 !
 application/x-rtp,media=video,encoding-name=VP8,payload=97 ! queue ! rtpvp8depay ! vp8dec ! 
queue ! videoconvert ! autovideosink sync=false
'''


from websockets.version import version as wsv
UNAVAILABLE_PEER_ID = -1
DEFAULT_SIGNALING_SERVER = 'wss://localhost:8443'

class WebRTCClient:
    def __init__(self, id_, peer_id, server):
        self.id_ = id_
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.peer_id = peer_id
        self.start_connection = self.peer_id == UNAVAILABLE_PEER_ID
        self.server = server or 'wss://webrtc.nirbheek.in:8443'


    async def connect(self):
        sslctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        self.conn = await websockets.connect(self.server, ssl=sslctx)
        await self.conn.send('HELLO %d' % self.id_)


    async def setup_call(self):
        await self.conn.send('SESSION {}'.format(self.peer_id))

    def send_sdp_offer(self, offer):
        text = offer.sdp.as_text()
        print ('Sending offer:\n%s' % text)
        msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(msg))
        loop.close()

    def on_offer_created(self, promise, _, __):
        print("__________2")
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')#reply['offer']
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def send_sdp_answer(self, answer):
        text = answer.sdp.as_text()
        print('Sending answer:\n%s' % text)
        message = json.dumps({'sdp': {'type': 'answer', 'sdp': text}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(message))
        loop.close()

    def on_answer_created(self, promise, _, __):
        print('Inner wainting 1...')
        promise.wait()
        print('Inner end wainting 1...')
        reply = promise.get_reply()
        answer = reply.get_value('answer')  # reply['answer']
        print('REPLY...', reply)
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', answer, promise)
        promise.interrupt()
        self.send_sdp_answer(answer)

    def on_negotiation_needed(self, element):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        icemsg = json.dumps({'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        print("Sending ice: ",icemsg)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(icemsg))
        loop.close()
    def manage_ice_candidate_message(self, _, mlineindex, candidate):
        icemsg = json.dumps({'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        print("Emitting:", icemsg)
        self.webrtc.emit("add-ice-candidate", mlineindex, candidate)
    def on_incoming_decodebin_stream(self, _, pad):
        return
        if not pad.has_current_caps():
            print (pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        gststruct = caps.get_structure(0)
        name = gststruct.get_name()

        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q)
            self.pipe.add( conv)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(resample)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_incoming_stream(self, _, pad):

        print("Incomming Stream")
        if pad.direction != Gst.PadDirection.SRC:
            return

        '''decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        self.webrtc.link(decodebin)'''

    def start_pipeline(self):
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        #self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.pipe.set_state(Gst.State.PLAYING)

    def handle_sdp(self, message):
        assert (self.webrtc)
        msg = json.loads(message)
        if 'sdp' in msg:
            sdp = msg['sdp']
            assert (sdp['type'] == 'offer')
            sdp = sdp['sdp']
            print('Received offer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', offer, promise)
            answer_promise = Gst.Promise.new_with_change_func(self.on_answer_created, None, None)
            print("FROM THERE")
            self.webrtc.emit("create-answer", None, answer_promise)
            print('Waiting...')
            answer_promise.wait()
            print('End of waiting')
            promise.interrupt()
        elif 'ice' in msg:
            ice = msg['ice']
            candidate = ice['candidate']
            sdpmlineindex = ice['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)

    def close_pipeline(self):
        self.pipe.set_state(Gst.State.NULL)
        self.pipe = None
        self.webrtc = None

    async def loop(self):
        assert self.conn
        self.start_pipeline()
        async for message in self.conn:
            if message == 'HELLO':
                if not self.start_connection:
                    await self.setup_call()
            elif message.startswith('ERROR'):
                print(message)
                self.close_pipeline()
                return 1
            else:
                if self.webrtc is not None:
                    self.handle_sdp(message)
            print(message)
        print("__________ CLOSING")
        self.close_pipeline()
        return 0

    async def stop(self):
        if self.conn:
            await self.conn.close()
        self.conn = None


def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True


if __name__=='__main__':
    Gst.init(None)
    if not check_plugins():
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument('peerid', nargs='?', default=UNAVAILABLE_PEER_ID, help='String ID of the peer to connect to')
    parser.add_argument('--server', nargs='?', default=DEFAULT_SIGNALING_SERVER, help='Signalling server to connect to, eg "wss://127.0.0.1:8443"')
    args = parser.parse_args()
    our_id = random.randrange(10, 10000)
    print("Our id is: ", our_id)
    c = WebRTCClient(our_id, args.peerid, args.server)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(c.connect())
    res = loop.run_until_complete(c.loop())
    sys.exit(res)
