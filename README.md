# JLR InControl to MQTT

A simple mqtt wrapper around [@ardevd's **jlrpy** library](https://github.com/ardevd/jlrpy), for accessing [Jaguar Land Rover's Remote Car API](https://documenter.getpostman.com/view/6250319/RznBMzqo?version=latest#intro).

Requires `paho-mqtt` and `python 3`. Configuration parameters for the mqtt broker etc are defined in a config file named `jlr2mqtt.cfg` (use the sample config file as a template).

Currently *all* status values are retrieved from JLR and posted to the mqtt broker (there are a lot of them!), though an attempt is made to categoriese them for the obvious ones.

Commands sent to the wrapper need to be JSON formatted, and have item `command` with a value set to the name of the API library function being called. Function names are exactly as defined in the `jlrpy.py` library. A single argument can be included with the key word `arg`. e.g: 

1. To set a departure time, the command would look like:
    `{"command":"add_departure_timer", "arg": "2019-10-31T15:10"}`

2. To refresh the current vehicle status:
    `{"command":"get_status"}`

Note that not all functions are directly supported, i.e. not all functions are pre-processed; they are called as is, along with the 
__single__ argument. In these cases, it is up to the user to ensure that the correctly formatted parameter is sent in the single arg (if at all possible).

The code also supports basic Home Assistant/openHAB MQTT discovery functionality, which can be enabled through the corresponding config file parameter (see the sample config file `jlr2mqtt.cfg.sample`).
