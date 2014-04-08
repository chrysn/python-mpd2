#idle command
#
#cf. official documentation for further details:
#
#http://www.musicpd.org/doc/protocol/ch02.html#id525963

import asyncio
import mpd

loop = asyncio.get_event_loop()
run = loop.run_until_complete

client = mpd.MPDClient()
run(client.connect('localhost', 6600))

for i in range(3):
    print(run(client.idle()))

# You can also run this inside a coroutine

@asyncio.coroutine
def main():
    for i in range(3):
        changes = yield from client.idle()
        print(changes)

loop.run_until_complete(main())
