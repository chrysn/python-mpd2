#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# IMPORTS
import asyncio
from mpd import (MPDClient, CommandError)
from random import choice
from socket import error as SocketError
from sys import exit

run = asyncio.get_event_loop().run_until_complete

## SETTINGS
##
HOST = 'localhost'
PORT = '6600'
PASSWORD = False
###


client = MPDClient()

try:
    run(client.connect(host=HOST, port=PORT))
except SocketError:
    exit(1)

if PASSWORD:
    try:
        run(client.password(PASSWORD))
    except CommandError:
        exit(1)

run(client.add(choice(list(run(client.list('file'))))))
client.disconnect()

# VIM MODLINE
# vim: ai ts=4 sw=4 sts=4 expandtab
