#!/usr/bin/python3

import mpd

def main():
    client = mpd.MPDClient()
    yield from client.connect("localhost", 6600)

    for entry in (yield from client.lsinfo("/")):
        print("%s" % entry)
    status = yield from client.status()
    for key, value in status.items():
        print("%s: %s" % (key, value))

if __name__ == "__main__":
    import asyncio
    asyncio.get_event_loop().run_until_complete(main())
