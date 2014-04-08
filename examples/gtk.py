#!/usr/bin/env python3

import sys

import asyncio
import mpd

from gi.repository import Gtk, GObject
import gbulb

def timelogging(generator, message="generator"):
    import time
    count = 0
    nexttime = 0
    yieldtime = 0
    while True:
        try:
            a = time.time()
            nextvalue = generator.__next__()
        except StopIteration:
            print("%s finished, spent %s in next() and %s in yield, in total %s cycles"%(message, nexttime, yieldtime, count))
            raise

        count += 1

        b = time.time()
        nexttime += b - a

        yield nextvalue

        yieldtime += time.time() - b

class MpClientWindow(Gtk.Window):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.client = mpd.MPDClient()

        vbox = Gtk.VBox()

        button = Gtk.Button("Connect")
        button.connect("clicked", self.on_connect)
        vbox.pack_start(button, True, True, 0)

        spinner = Gtk.Spinner()
        spinner.start()
        vbox.pack_start(spinner, True, True, 0)

        self.add(vbox)

    def on_connect(self, button):
        self.connecting = asyncio.async(self.mpd_connect())
        def done(result):
            # make sure the exception does not get lost
            print("done, result is %s"%result)
            if result.exception() is not None:
                import traceback
                traceback.print_tb(result.exception().__traceback__)
        self.connecting.add_done_callback(done)

    @asyncio.coroutine
    def mpd_connect(self):
        try:
            self.client.disconnect()
        except mpd.ConnectionError:
            pass

        yield from self.client.connect('localhost', 6600)
        print("connected")

        resultyielder = self.client.repeat(0)
        # harr, dropping the future. does not matter.
        # print("repeat=0 yielded %r"%(yield from resultyielder))

        print("current song: %r"%(yield from self.client.currentsong()))
        print("status: %r"%(yield from self.client.status()))

        result = self.client.listall()
        for cursor in (yield from result.lines):
            listed = yield from cursor
            print('.', end="")
            sys.stdout.flush()
        print("\n")

        print("screwing with sequence")

        status = self.client.status()
        print("current song: %r"%(yield from self.client.currentsong()))
        print("status: %r"%(yield from status))

        print("using command sequences")
        self.client.command_list_ok_begin()
        cs = self.client.currentsong()
        s = self.client.status()
        yield from self.client.command_list_end()

        print("current song: %r"%(yield from cs))
        print("status: %r"%(yield from s))

        print("idle events: %r"%(yield from self.client.idle()))

        return 42

@asyncio.coroutine
def newmain():
    proto = yield from mpd.MPDProtocol.open_connection('127.0.0.1', 6600)

    cs = yield from proto.send_command("currentsong")
    print("cs", cs)

    #s = proto.send_command("idle")
    s = proto.send_command("status")
    for cursor in (yield from s.lines):
        line = yield from cursor
        print("result from cursor received: %r"%line)
    print("that's all")

asyncio.set_event_loop_policy(gbulb.GtkEventLoopPolicy())

win = MpClientWindow()
win.connect("delete-event", Gtk.main_quit)
win.show_all()

loop = asyncio.get_event_loop()
#loop.run_until_complete(newmain())

# workaround for main loop
loop.call_exception_handler = lambda context: print("EXCEPTION? in %r."%context)

loop.run_forever()
