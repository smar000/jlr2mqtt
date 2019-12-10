# JLR InControl to MQTT

A simple mqtt wrapper around [@ardevd's **jlrpy** library](https://github.com/ardevd/jlrpy), for accessing [Jaguar Land Rover's Remote Car API](https://documenter.getpostman.com/view/6250319/RznBMzqo?version=latest#intro).

Requires `paho-mqtt` and `python 3`. Configuration parameters for the mqtt broker etc are defined in a config file named `jlr2mqtt.cfg` (use the sample config file as a template).

Currently *all* status values are retrieved from JLR and posted to the mqtt broker (there are a lot of them!), though an attempt is made to categoriese them for the obvious ones.

Commands sent to the wrapper need to be JSON formatted, and have item `command` with a value set to the name of the API library function being called. Function names are exactly as defined in the `jlrpy.py` library. All arguments required by the underlying function *must* be provided as a dict comprising of the parameter name and its value under the parent key "kwargs" (this can be omitted if the function has no arguments). The only exception is for the 'pin' parameter required by a number of the functions. If this is defined in the config file (see below), then it is not necessary to specify it in the `kwargs` dict.

Some examples:

1. To set a departure time, the command would look like:
    `{"command":"add_departure_timer", "kwargs": {"index": 0, "year": 2019, "month": 10, "day": 31", "hour": 15, "minute": 0} }`

2. To turn the engine on and start the remote climate control:
    `{"command":"remote_engine_start", "kwargs": {"pin": 1234, "target_value": 42} }`

3. To refresh the current vehicle status values:
    `{"command":"get_status"}`

The code also supports basic Home Assistant/openHAB MQTT discovery functionality, which can be enabled through the corresponding config file parameter (see the sample config file `jlr2mqtt.cfg.sample`). 

Finally, the code is made available here as is, no warranties of any kind whatsoever etc etc. It is currently at a level that suits my needs, and so I am not likely to be spending much further time on it.
