# Sleep when idle

`sleep-when-idle` is a daemon to initiate a transition to a sleep state when the system is idle.

## Purpose

It can be used on a headless server, or on a desktop (in that case, you should disable the transition to sleep in your desktop environment).

The `sleep-when-idle` daemon gives you more flexibility on WHEN the system is considered idle. 

Desktop environments perform a transition to sleep when there is no input from the user.
But the machine can have on-going tasks that shall maintain the machine alive until completion:
- long download from Internet or the network
- media served to a TV
- audio playback
- heavy CPU task (compilation, compression...)

The `sleep-when-idle` daemon will take these use cases in consideration.

## Features

Here are the possible elements monitored by `sleep-when-idle` to determine whether the system is idle:
- no X user inputs (requires `xprintidle`)
- idle CPU time
- network traffic
- audio output

You can also configure a wake-up time (requires `rtcwake`) to perform daily tasks.

## Install

1. See `sleep-when-idle.py -h` for the various options.
2. Copy `sleep-when-idle.service` to `/etc/systemd/system`
3. Edit the file to enable the wanted options
4. Enable the service: `systemctl enable sleep-when-idle`

## Runtime dependencies

- `systemd` for transition to sleep
- `rtcwake` to set a time for wake-up
- `xprintidle` to check user inputs to the X server
- `pulseaudio` to check for active audio output

## Thanks

- ferncasado/DanglingPointer for the [original idea](https://launchpad.net/keep.awake)
- the whole open source community for all the wonderful tools we are using everyday
