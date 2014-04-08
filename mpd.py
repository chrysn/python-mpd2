# python-mpd2: Python MPD client library
# Copyright (C) 2008-2010  J. Alexander Treuman <jat@spatialrift.net>
# Copyright (C) 2012  J. Thalheim <jthalheim@gmail.com>
#
# python-mpd2 is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# python-mpd2 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with python-mpd2.  If not, see <http://www.gnu.org/licenses/>.

# asyncio porting TODO:
#
# * unix sockets
# * not yet tested, probably don't work
#   * idle (works, but cancelling the task does not send a noidle command)
#   * command lists
#   * iterate
# * act on all those "form into generator shape again" todos (enabling iterate)

import logging
import sys
import socket
import warnings
from collections import Callable
import asyncio
import asyncio.streams
from asyncio import coroutine

VERSION = (0, 5, 3)
HELLO_PREFIX = "OK MPD "
ERROR_PREFIX = "ACK "
SUCCESS = "OK"
NEXT = "list_OK"

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

class MPDError(Exception):
    pass

class ConnectionError(MPDError):
    pass

class ProtocolError(MPDError):
    pass

class CommandError(MPDError):
    pass

class CommandListError(MPDError):
    pass

class PendingCommandError(MPDError):
    pass

class IteratingError(MPDError):
    pass


class _NotConnected(object):
    def __getattr__(self, attr):
        return self._dummy

    def _dummy(*args):
        raise ConnectionError("Not connected")

_commands = {
    # Status Commands
    "clearerror":         "_fetch_nothing",
    "currentsong":        "_fetch_object",
    "idle":               "_fetch_idle",
    "status":             "_fetch_object",
    "stats":              "_fetch_object",
    # Playback Option Commands
    "consume":            "_fetch_nothing",
    "crossfade":          "_fetch_nothing",
    "mixrampdb":          "_fetch_nothing",
    "mixrampdelay":       "_fetch_nothing",
    "random":             "_fetch_nothing",
    "repeat":             "_fetch_nothing",
    "setvol":             "_fetch_nothing",
    "single":             "_fetch_nothing",
    "replay_gain_mode":   "_fetch_nothing",
    "replay_gain_status": "_fetch_item",
    # Playback Control Commands
    "next":               "_fetch_nothing",
    "pause":              "_fetch_nothing",
    "play":               "_fetch_nothing",
    "playid":             "_fetch_nothing",
    "previous":           "_fetch_nothing",
    "seek":               "_fetch_nothing",
    "seekid":             "_fetch_nothing",
    "seekcur":            "_fetch_nothing",
    "stop":               "_fetch_nothing",
    # Playlist Commands
    "add":                "_fetch_nothing",
    "addid":              "_fetch_item",
    "clear":              "_fetch_nothing",
    "delete":             "_fetch_nothing",
    "deleteid":           "_fetch_nothing",
    "move":               "_fetch_nothing",
    "moveid":             "_fetch_nothing",
    "playlist":           "_fetch_playlist",
    "playlistfind":       "_fetch_songs",
    "playlistid":         "_fetch_songs",
    "playlistinfo":       "_fetch_songs",
    "playlistsearch":     "_fetch_songs",
    "plchanges":          "_fetch_songs",
    "plchangesposid":     "_fetch_changes",
    "prio":               "_fetch_nothing",
    "prioid":             "_fetch_nothing",
    "shuffle":            "_fetch_nothing",
    "swap":               "_fetch_nothing",
    "swapid":             "_fetch_nothing",
    # Stored Playlist Commands
    "listplaylist":       "_fetch_list",
    "listplaylistinfo":   "_fetch_songs",
    "listplaylists":      "_fetch_playlists",
    "load":               "_fetch_nothing",
    "playlistadd":        "_fetch_nothing",
    "playlistclear":      "_fetch_nothing",
    "playlistdelete":     "_fetch_nothing",
    "playlistmove":       "_fetch_nothing",
    "rename":             "_fetch_nothing",
    "rm":                 "_fetch_nothing",
    "save":               "_fetch_nothing",
    # Database Commands
    "count":              "_fetch_object",
    "find":               "_fetch_songs",
    "findadd":            "_fetch_nothing",
    "list":               "_fetch_list",
    "listall":            "_fetch_database",
    "listallinfo":        "_fetch_database",
    "lsinfo":             "_fetch_database",
    "readcomments":       "_fetch_object",
    "search":             "_fetch_songs",
    "searchadd":          "_fetch_nothing",
    "searchaddpl":        "_fetch_nothing",
    "update":             "_fetch_item",
    "rescan":             "_fetch_item",
    # Sticker Commands
    "sticker get":        "_fetch_sticker",
    "sticker set":        "_fetch_nothing",
    "sticker delete":     "_fetch_nothing",
    "sticker list":       "_fetch_stickers",
    "sticker find":       "_fetch_songs",
    # Connection Commands
    "close":              None,
    "kill":               None,
    "password":           "_fetch_nothing",
    "ping":               "_fetch_nothing",
    # Audio Output Commands
    "disableoutput":      "_fetch_nothing",
    "enableoutput":       "_fetch_nothing",
    "toggleoutput":       "_fetch_nothing",
    "outputs":            "_fetch_outputs",
    # Reflection Commands
    "config":             "_fetch_item",
    "commands":           "_fetch_list",
    "notcommands":        "_fetch_list",
    "tagtypes":           "_fetch_list",
    "urlhandlers":        "_fetch_list",
    "decoders":           "_fetch_plugins",
    # Client To Client
    "subscribe":          "_fetch_nothing",
    "unsubscribe":        "_fetch_nothing",
    "channels":           "_fetch_list",
    "readmessages":       "_fetch_messages",
    "sendmessage":        "_fetch_nothing",
}

class MultilineFuture(asyncio.Future):
    """A future that returns a list of lines, but also has a .nextline property
    that can be yielded from and gives line-wise results.

    Lines will be delayed until the next line arrives. Thus, you can use it
    like

    >>> f = MultilineFuture()
    >>> for cursor in (yield from f.lines):
    >>>     line = yield from cursor

    and the iteration will stop with the last item when fed via
    `f.send_line(line)` and `f.set_completed()`."""

    def __init__(self):
        super().__init__()
        self.backlog = []
        self._nextfuture = asyncio.Future()

    @coroutine
    def _receive(self):
        """Async-block until an element has been appended to the backlog."""
        yield from self._nextfuture
        self._nextfuture = asyncio.Future()

    @property
    @coroutine
    def lines(self):
        it = self._cursor()

        if self.backlog:
            return it
        else:
            try:
                yield from self._receive()
            except asyncio.CancelledError:
                return ()
            return it

    def _cursor(self):
        """Iterator that yields a future which waits for the backlog to contain
        an element, so the iterator can always generate an element that is safe
        to yield from without running into cancellation."""

        while True:
            if len(self.backlog) > 1 or (self.backlog and self.done()):
                f = asyncio.Future()
                f.set_result(self.backlog.pop(0))
                yield f
                continue
            elif self.done():
                return

            f = asyncio.Future()
            # no matter whether _nextfuture works or fails, in all cases we have
            # a backlog item left; and whoever set _nextfuture hopefully also
            # made self done.
            self._nextfuture.add_done_callback(lambda s: f.set_result(self.backlog.pop(0)))
            yield f

            if self._nextfuture.done():
                self._nextfuture = asyncio.Future()

    @coroutine
    def conext(self):
        """As an alternative to the cursor = yield from .lines / line = yield
        from cursor construction, the MultilineFuture offers a .conext() method
        in analogy to .next(). As the StopIteration that would normally be
        passed out if the generator is exhausted is used internally in
        generators, MultilineFuture.StopIteration has to be caught instead."""

        if self.backlog:
            return self.backlog.pop(0)

        if self.done():
            raise self.StopIteration()

        try:
            yield from self._receive()
        except asyncio.CancelledError:
            raise self.StopIteration()

        return self.backlog.pop(0)

    class StopIteration(BaseException):
        """Exception for catching MultilineFuture.conext() end-of-iterator
        situations."""

    def send_line(self, line):
        self.backlog.append(line)
        if not self._nextfuture.done():
            self._nextfuture.set_result(None)

    def set_completed(self):
        if not self._nextfuture.done():
            self._nextfuture.cancel()
        self.set_result(self.backlog)

    def set_exception(self, exc):
        if not self._nextfuture.done():
            self._nextfuture.set_exception(exc)
        super().set_exception(exc)

class MPDProtocol(asyncio.StreamReaderProtocol):
    @classmethod
    @coroutine
    def open_connection(cls, host=None, port=6600, loop=None):
        """A asyncio.streams.open_connection style method for MPD"""
        if loop is None:
            loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader(loop=loop)
        protocol = cls(reader, loop=loop)
        yield from loop.create_connection(lambda: protocol, host, port)
        return protocol

    def __init__(self, stream_reader, loop=None):
        super().__init__(stream_reader, self.main_loop, loop=loop)

        # this property group will be more important when the
        # pause_/resume_writing calls are implemented. then, there can be an
        # async-blocking send_command too, which will wait for the send queue
        # to be free (halting control flow in whatever generates that many
        # command writes instead of appending and appending to the queue).
        self._writer = None
        self._can_send = False
        self._command_backlog = []

        self._processorqueue = []
        self._processorqueue_next = asyncio.Future()

    @coroutine
    def main_loop(self, reader, writer):
        self.first_line = yield from reader.readline()

        self._writer = writer
        self._can_send = True
        while self._command_backlog:
            writer.write(self._command_backlog.pop(0))

        while True:
            while not self._processorqueue:
                yield from self._processorqueue_next
                self._processorqueue_next = asyncio.Future()

            command_processor = self._processorqueue.pop(0)

            while not command_processor.done():
                l = (yield from reader.readline()).decode('utf8').rstrip('\n')
                if l.strip() == SUCCESS:
                    command_processor.set_completed()
                elif l.startswith(ERROR_PREFIX):
                    command_processor.set_exception(CommandError(l))
                else:
                    command_processor.send_line(l)

    def send_command(self, command):
        """Immediately send the `command`, and return a MultilineFuture that
        will contain the command's response."""

        self.send_line(command)

        return self.enqueue_command_processor()

    def send_line(self, line):
        """Like send_command, but will not enqueue a new command processor. Use
        only when you explicitly enqueue a processor afterwards."""

        line_encoded = ("%s\n"%line).encode('utf8')

        if self._can_send:
            self._writer.write(line_encoded)
        else:
            self._command_backlog.append(line_encoded)

    def enqueue_command_processor(self):
        """Call this whenever you sent a command on its way; it returns a
        MultilineFuture that will yield the responses."""

        f = MultilineFuture()
        self._processorqueue.append(f)
        if not self._processorqueue_next.done():
            self._processorqueue_next.set_result(None)

        return f



class MPDClient(object):
    def __init__(self):
        self._reset()

    def _execute(self, command, args, retval):
        if self._command_list is not None:
            raise NotImplementedError() # yield from
            if not isinstance(retval, Callable):
                raise CommandListError("'%s' not allowed in command list" %
                                        command)
            self._write_command(command, args)
            self._command_list.append(retval)
        else:
            self._write_command(command, args)
            return retval(self._protocol.enqueue_command_processor())

    def _write_command(self, command, args=[]):
        parts = [command]
        for arg in args:
            if type(arg) is tuple:
                if len(arg) == 1:
                    parts.append('"%d:"' % int(arg[0]))
                else:
                    parts.append('"%d:%d"' % (int(arg[0]), int(arg[1])))
            else:
                parts.append('"%s"' % escape(str(arg)))
        # Minimize logging cost if the logging is not activated.
        if logger.isEnabledFor(logging.DEBUG):
            if command == "password":
                logger.debug("Calling MPD password(******)")
            else:
                logger.debug("Calling MPD %s%r", command, args)
        self._protocol.send_line(" ".join(parts))

    def _read_pair(self, lines, separator):
        try:
            line = yield from lines.conext()
        except lines.StopIteration:
            return None
        pair = line.split(separator, 1)
        if len(pair) < 2:
            raise ProtocolError("Could not parse pair: '%s'" % line)
        return pair

    def _read_pairs(self, lines, separator=": "):
        result = []
        pair = yield from self._read_pair(lines, separator)
        while pair:
            result.append(pair)
            pair = yield from self._read_pair(lines, separator)
        ## @todo form into generator shape again
        return result

    def _read_list(self, lines):
        seen = None
        result = []
        for key, value in (yield from self._read_pairs(lines)):
            if key != seen:
                if seen is not None:
                    raise ProtocolError("Expected key '%s', got '%s'" %
                                        (seen, key))
                seen = key
            result.append(value)
        ## @todo form into generator shape again
        return result

    def _read_playlist(self):
        result = []
        for key, value in (yield from self._read_pairs(":")):
            result.append(value)
        ## @todo form into generator shape again
        return result

    def _read_objects(self, lines, delimiters=[]):
        result = MultilineFuture()
        @coroutine
        def worker(self=self, lines=lines, delimiters=delimiters, result=result):
            obj = {}
            for key, value in (yield from self._read_pairs(lines)):
                key = key.lower()
                if obj:
                    if key in delimiters:
                        result.send_line(obj)
                        obj = {}
                    elif key in obj:
                        if not isinstance(obj[key], list):
                            obj[key] = [obj[key], value]
                        else:
                            obj[key].append(value)
                        continue
                obj[key] = value
            if obj:
                result.send_line(obj)
            result.set_completed()
        asyncio.async(worker())
        return result

    def _read_stickers(self):
        result = []
        for key, sticker in (yield from self._read_pairs()):
            value = sticker.split('=', 1)

            if len(value) < 2:
                raise ProtocolError("Could not parse sticker: %r" % sticker)

            result.append(value)
        return result

    @coroutine
    def _fetch_nothing(self, lines):
        try:
            line = yield from lines.conext()
        except lines.StopIteration:
            pass
        else:
            raise ProtocolError("Got unexpected return value: '%s'" % line)

    def _fetch_item(self):
        pairs = list((yield from self._read_pairs()))
        if len(pairs) != 1:
            return
        return pairs[0][1]

    def _fetch_sticker(self):
        # Either we get one or we get an error while reading the line
        key, value = list(self._read_stickers())[0]
        return value

    def _fetch_stickers(self):
        return dict(self._read_stickers())

    def _fetch_list(self, lines):
        return self._read_list(lines)

    def _fetch_playlist(self):
        return self._read_playlist()

    @coroutine
    def _fetch_object(self, lines):
        objs = list((yield from self._read_objects(lines)))
        if not objs:
            return {}
        return objs[0]

    def _fetch_changes(self):
        return self._read_objects(["cpos"])

    def _fetch_idle(self, lines):
        ret = self._fetch_list(lines)
        return ret

    def _fetch_songs(self, lines):
        return self._read_objects(lines, ["file"])

    def _fetch_playlists(self, lines):
        return self._read_objects(lines, ["playlist"])

    def _fetch_database(self, lines):
        return self._read_objects(lines, ["file", "directory", "playlist"])

    def _fetch_messages(self, lines):
        return self._read_objects(lines, ["channel"])

    def _fetch_outputs(self, lines):
        return self._read_objects(lines, ["outputid"])

    def _fetch_plugins(self, lines):
        return self._read_objects(lines, ["plugin"])

    def noidle(self):
        self._write_command("noidle")
        return self._fetch_list()

    def _hello(self):
        line = self._protocol.first_line
        if not line.endswith("\n"):
            self.disconnect()
            raise ConnectionError("Connection lost while reading MPD hello")
        line = line.rstrip("\n")
        if not line.startswith(HELLO_PREFIX):
            raise ProtocolError("Got invalid MPD hello: '%s'" % line)
        self.mpd_version = line[len(HELLO_PREFIX):].strip()

    def _reset(self):
        self.mpd_version = None
        self._command_list = None
        self._protocol = None

    def _connect_unix(self, path):
        if not hasattr(socket, "AF_UNIX"):
            raise ConnectionError("Unix domain sockets not supported "
                                  "on this platform")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(path)
        return sock

    idletimeout = None

    @coroutine
    def connect(self, host, port=6600):
        logger.info("Calling MPD connect(%r, %)", host, port)
        if self._protocol is not None:
            raise ConnectionError("Already connected")

        if host.startswith("/"):
            self._protocol = self._connect_unix(host)
        else:
            self._protocol = yield from MPDProtocol.open_connection(host, port)

        # FIXME: not sure whether it's really guaranteed that the first step of
        # the main loop has already been run
        #self._hello()

    def disconnect(self):
        logger.info("Calling MPD disconnect()")

        if self._protocol is not None:
            self._protocol.disconnect()

        self._reset()

    def fileno(self):
        if self._sock is None:
            raise ConnectionError("Not connected")
        return self._sock.fileno()

    def command_list_ok_begin(self):
        if self._command_list is not None:
            raise CommandListError("Already in command list")
        self._write_command("command_list_ok_begin")
        self._command_list = []

    def command_list_end(self):
        if self._command_list is None:
            raise CommandListError("Not in command list")
        self._write_command("command_list_end")
        cl = self._command_list
        self._command_list = None
        return self._command_list_dispatch(cl, self._protocol.enqueue_command_processor())

    @coroutine
    def _command_list_dispatch(self, cl, linefuture):
        command = cl.pop(0) if cl else None

        for cursor in (yield from linefuture):
            line = yield from cursor

            if command is None:
                raise ProtocolError("Got unexpected '%r' in command list"%line)

            if command.strip() == NEXT:
                cl.set_completed()
                command = cl.pop(0) if cl else None
                continue

            cl.send_line(line)
        if cl is not None:
            raise ProtocolError("Unexpected end of command list output")

    @classmethod
    def add_command(cls, name, callback):
        method = newFunction(cls._execute, key, callback)
        escaped_name = name.replace(" ", "_")
        setattr(cls, escaped_name, method)

    @classmethod
    def remove_command(cls, name):
        if not hasattr(cls, name):
            raise ValueError("Can't remove not existent '%s' command" % name)
        name = name.replace(" ", "_")
        delattr(cls, str(name))
        delattr(cls, str("send_" + name))
        delattr(cls, str("fetch_" + name))

def bound_decorator(self, function):
    """ bind decorator to self """
    if not isinstance(function, Callable):
        return None

    def decorator(*args, **kwargs):
        return function(self, *args, **kwargs)
    return decorator

def newFunction(wrapper, name, returnValue):
    def decorator(self, *args):
        return wrapper(self, name, args, bound_decorator(self, returnValue))
    return decorator

for key, value in _commands.items():
    returnValue = None if value is None else MPDClient.__dict__[value]
    MPDClient.add_command(key, returnValue)

def escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


# vim: set expandtab shiftwidth=4 softtabstop=4 textwidth=79:
