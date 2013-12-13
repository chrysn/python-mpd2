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

IS_PYTHON2 = sys.version_info < (3, 0)
if IS_PYTHON2:
    decode_str = lambda s: s.decode("utf-8")
    encode_str = lambda s: s if type(s) == str else (unicode(s)).encode("utf-8")
else:
    decode_str = lambda s: s
    encode_str = lambda s: str(s)

try:
    from logging import NullHandler
except ImportError: # NullHandler was introduced in python2.7
    class NullHandler(logging.Handler):
        def emit(self, record):
            pass

logger = logging.getLogger(__name__)
logger.addHandler(NullHandler())

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

class LineEater(asyncio.Future):
    """Future on a generator that gets fed lines

    The LineEater is a Future that can be used to dispatch lines in a protocol
    where different routines take turns in consuming lines, but only those
    routines can determine whether they are done with it. (That's not exactly
    the case in the MPD protocol, but makes adapting the current implementation
    easier).

    The generators used here are *not* coroutines -- while coroutines yield
    futures (for whose completion they wait and whose values they get passed
    back in), these generators yield nothing, and wait for a next line to be
    passed in. This allows orderly execution: When it is a LineEater's turn to
    consume lines, `feed` it lines until it returns False, which means that the
    generator has ended and that the next eater in line should receive
    subsequent lines.

    When the generator exits, its result will be used to finish the future
    which the LineEater is. (In case of an error, that will set an exception on
    the future).

    Note that the generators inside a LineEater can easily nest using `yield
    from` the same way a asyncio.coroutine nests. Instead of an innermost
    `yield from my_socket.read()` (which leads to competition among tasks),
    there is simply a `yield`.
    """

    def __init__(self, generator):
        super().__init__()

        self._generator = generator

    def feed(self, line):
        try:
            self._generator.send(line)
        except StopIteration as s:
            self.set_result(s.value)
            return False
        except Exception as e:
            self.set_exception(e)
            return False

        return True

class MPDClient(object):
    def __init__(self, use_unicode=False):
        self.iterate = False
        self.use_unicode = use_unicode
        self._readtask = None
        self._reset()

    def _send(self, command, args, retval):
        if self._command_list is not None:
            raise CommandListError("Cannot use send_%s in a command list" %
                                   command)
        self._write_command(command, args)
        if retval is not None:
            self._pending.append(command)

    def _fetch(self, command, args, retval):
        if self._command_list is not None:
            raise CommandListError("Cannot use fetch_%s in a command list" %
                                   command)
        if self._iterating:
            raise IteratingError("Cannot use fetch_%s while iterating" %
                                 command)
        if not self._pending:
            raise PendingCommandError("No pending commands to fetch")
        if self._pending[0] != command:
            raise PendingCommandError("'%s' is not the currently "
                                      "pending command" % command)
        del self._pending[0]
        if isinstance(retval, Callable):
            return retval()
        return retval

    def _execute(self, command, args, retval):
        if self._iterating:
            raise IteratingError("Cannot execute '%s' while iterating" %
                                 command)
        if self._pending:
            raise PendingCommandError("Cannot execute '%s' with "
                                      "pending commands" % command)
        if self._command_list is not None:
            raise NotImplementedError() # yield from
            if not isinstance(retval, Callable):
                raise CommandListError("'%s' not allowed in command list" %
                                        command)
            self._write_command(command, args)
            self._command_list.append(retval)
        else:
            self._write_command(command, args)
            if isinstance(retval, Callable):
                ## @todo implement any other case
                return (yield from retval())
            raise NotImplementedError() # yield from
            return retval

    def _execute_async(self, command, args, retval):
        """Append the command and create a LineEater from the retval function.
        See the LineEater documentation on how those should behave."""
        self._pending = None # destroy whatever uses it so things break early.
                             # those don't go together.
        self._write_command(command, args)

        generator = retval()
        generator.__next__() # start generator

        result = LineEater(generator)

        self._lineeaters.append(result)

        return result

    @coroutine
    def _feed_lineeaters(self):
        while True:
            line = yield from self._read_line()
            if not self._lineeaters:
                self.disconnect()
                raise ConnectionError("Unexpected chatting")
            want_more = self._lineeaters[0].feed(line)
            if not want_more:
                self._lineeaters.pop(0)

    def _write_line(self, line):
        self._wfile.write(("%s\n" % line).encode('utf-8'))
        #self._wfile.flush()

    def _write_command(self, command, args=[]):
        parts = [command]
        for arg in args:
            if type(arg) is tuple:
                if len(arg) == 1:
                    parts.append('"%d:"' % int(arg[0]))
                else:
                    parts.append('"%d:%d"' % (int(arg[0]), int(arg[1])))
            else:
                parts.append('"%s"' % escape(encode_str(arg)))
        # Minimize logging cost if the logging is not activated.
        if logger.isEnabledFor(logging.DEBUG):
            if command == "password":
                logger.debug("Calling MPD password(******)")
            else:
                logger.debug("Calling MPD %s%r", command, args)
        self._write_line(" ".join(parts))

    def _read_line(self):
        ## @todo things block anyway because readline is inefficient when
        # dealing with more data
        line = (yield from self._rfile.readline()).decode('utf-8')
        if self.use_unicode:
            line = decode_str(line)
        if not line.endswith("\n"):
            self.disconnect()
            raise ConnectionError("Connection lost while reading line")
        line = line.rstrip("\n")
        if line.startswith(ERROR_PREFIX):
            error = line[len(ERROR_PREFIX):].strip()
            raise CommandError(error)
        if self._command_list is not None:
            if line == NEXT:
                return
            if line == SUCCESS:
                raise ProtocolError("Got unexpected '%s'" % SUCCESS)
        elif line == SUCCESS:
            return
        return line

    def _read_pair(self, separator):
        line = yield
        if line is None:
            return
        pair = line.split(separator, 1)
        if len(pair) < 2:
            raise ProtocolError("Could not parse pair: '%s'" % line)
        return pair

    def _read_pairs(self, separator=": "):
        result = []
        pair = yield from self._read_pair(separator)
        while pair:
            result.append(pair)
            pair = yield from self._read_pair(separator)
        ## @todo form into generator shape again
        return result

    def _read_list(self):
        seen = None
        result = []
        for key, value in (yield from self._read_pairs()):
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

    def _read_objects(self, delimiters=[]):
        result = []
        obj = {}
        for key, value in (yield from self._read_pairs()):
            key = key.lower()
            if obj:
                if key in delimiters:
                    result.append(obj)
                    obj = {}
                elif key in obj:
                    if not isinstance(obj[key], list):
                        obj[key] = [obj[key], value]
                    else:
                        obj[key].append(value)
                    continue
            obj[key] = value
        if obj:
            result.append(obj)
        ## @todo form into generator shape again
        return result

    def _read_command_list(self):
        try:
            for retval in self._command_list:
                yield retval()
        finally:
            self._command_list = None
        self._fetch_nothing()

    def _read_stickers(self):
        result = []
        for key, sticker in (yield from self._read_pairs()):
            value = sticker.split('=', 1)

            if len(value) < 2:
                raise ProtocolError("Could not parse sticker: %r" % sticker)

            result.append(value)
        return result

    def _iterator_wrapper(self, iterator):
        try:
            for item in iterator:
                yield item
        finally:
            self._iterating = False

    def _wrap_iterator(self, iterator):
        ## @todo make this actually usable with asyncio
        if not self.iterate:
            return iterator
        self._iterating = True
        return self._iterator_wrapper(iterator)

    def _fetch_nothing(self):
        line = yield
        if line is not None:
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

    def _fetch_list(self):
        return self._wrap_iterator(self._read_list())

    def _fetch_playlist(self):
        return self._wrap_iterator(self._read_playlist())

    def _fetch_object(self):
        objs = list((yield from self._read_objects()))
        if not objs:
            return {}
        return objs[0]

    def _fetch_objects(self, delimiters):
        return self._wrap_iterator(self._read_objects(delimiters))

    def _fetch_changes(self):
        return self._fetch_objects(["cpos"])

    def _fetch_idle(self):
        #self._sock.settimeout(self.idletimeout)
        ret = self._fetch_list()
        #self._sock.settimeout(self._timeout)
        return ret

    def _fetch_songs(self):
        return self._fetch_objects(["file"])

    def _fetch_playlists(self):
        return self._fetch_objects(["playlist"])

    def _fetch_database(self):
        return self._fetch_objects(["file", "directory", "playlist"])

    def _fetch_messages(self):
        return self._fetch_objects(["channel"])

    def _fetch_outputs(self):
        return self._fetch_objects(["outputid"])

    def _fetch_plugins(self):
        return self._fetch_objects(["plugin"])

    def _fetch_command_list(self):
        return self._wrap_iterator(self._read_command_list())

    def noidle(self):
        if not self._pending or self._pending[0] != 'idle':
          raise CommandError('cannot send noidle if send_idle was not called')
        del self._pending[0]
        self._write_command("noidle")
        return self._fetch_list()

    def _hello(self):
        line = (yield from self._rfile.readline()).decode('utf-8')
        if not line.endswith("\n"):
            self.disconnect()
            raise ConnectionError("Connection lost while reading MPD hello")
        line = line.rstrip("\n")
        if not line.startswith(HELLO_PREFIX):
            raise ProtocolError("Got invalid MPD hello: '%s'" % line)
        self.mpd_version = line[len(HELLO_PREFIX):].strip()

    def _reset(self):
        self.mpd_version = None
        self._iterating = False
        self._pending = []
        self._command_list = None
        self._lineeaters = []
        self._sock = None
        self._rfile = _NotConnected()
        self._wfile = _NotConnected()

    def _connect_unix(self, path):
        if not hasattr(socket, "AF_UNIX"):
            raise ConnectionError("Unix domain sockets not supported "
                                  "on this platform")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(path)
        return sock

    @coroutine
    def _connect_tcp(self, host, port):
        self._sock = True

        try:
            self._rfile, self._wfile = yield from asyncio.streams.open_connection(host, port)
            ## @todo set sockopt SO_KEEPALIVE, and set socket timeout
        except:
            self._reset()
            raise

    def _settimeout(self, timeout):
        self._timeout = timeout
        if self._sock != None:
            ## @todo set timeout
            pass
    def _gettimeout(self):
        return self._timeout
    timeout = property(_gettimeout, _settimeout)
    _timeout = None
    idletimeout = None

    def connect(self, host, port, timeout=None):
        logger.info("Calling MPD connect(%r, %r, timeout=%r)", host,
                     port, timeout)
        if self._sock is not None:
            raise ConnectionError("Already connected")
        if timeout != None:
            warnings.warn("The timeout parameter in connect() is deprecated! "
                          "Use MPDClient.timeout = yourtimeout instead.",
                          DeprecationWarning)
            self.timeout = timeout
        if host.startswith("/"):
            self._sock = self._connect_unix(host)
        else:
            yield from self._connect_tcp(host, port)

        try:
            yield from self._hello()
        except Exception as e:
            print("something went wrong: %s"%e)
            ## @todo unqualified except!
            self.disconnect()
            raise

        self._readtask = asyncio.Task(self._feed_lineeaters())

    def disconnect(self):
        logger.info("Calling MPD disconnect()")

        if self._readtask:
            self._readtask.cancel()

        if not self._wfile is None:
            self._wfile.close()
        self._reset()

    def fileno(self):
        if self._sock is None:
            raise ConnectionError("Not connected")
        return self._sock.fileno()

    def command_list_ok_begin(self):
        if self._command_list is not None:
            raise CommandListError("Already in command list")
        if self._iterating:
            raise IteratingError("Cannot begin command list while iterating")
        if self._pending:
            raise PendingCommandError("Cannot begin command list "
                                      "with pending commands")
        self._write_command("command_list_ok_begin")
        self._command_list = []

    def command_list_end(self):
        if self._command_list is None:
            raise CommandListError("Not in command list")
        if self._iterating:
            raise IteratingError("Already iterating over a command list")
        self._write_command("command_list_end")
        return self._fetch_command_list()

    @classmethod
    def add_command(cls, name, callback):
        method = newFunction(cls._execute, key, callback)
        send_method = newFunction(cls._send, key, callback)
        fetch_method = newFunction(cls._fetch, key, callback)
        async_method = newFunction(cls._execute_async, key, callback)

        # create new mpd commands as function in three flavors:
        # normal, with "send_"-prefix and with "fetch_"-prefix
        escaped_name = name.replace(" ", "_")
        #setattr(cls, escaped_name, method)
        #setattr(cls, "send_"+escaped_name, send_method)
        #setattr(cls, "fetch_"+escaped_name, fetch_method)

        # it might be worth considering to have a MPDClient class and a
        # MPDAsyncClient class, which differ in that the async methods are
        # hidden groundwork in the compatibility MPDClient, which instead
        # exposes wrapper methods
        setattr(cls, escaped_name, async_method)

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
