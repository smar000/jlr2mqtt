"""
A simple mqtt wrapper around @ardevd's 'jlrpy' python library (https://github.com/ardevd/jlrpy), for accessing Jaguar Land Rover's 
Remote Car API (https://documenter.getpostman.com/view/6250319/RznBMzqo?version=latest#intro).

Requires paho-mqtt and python 3.

Commands sent to the wrapper need to be jason formatted, and have item `command` with a value set to the name of the API library function
being called. Function names are exactly as defined in the `jlrpy.py` library, with arguments given with named parameters in the dict 'kwargs' e.g:

    1. To set a departure time, the command would look like:
        {"command":"add_departure_timer", "kwargs": {"index": 0, "year": 2019, "month": 10, "day": 31", "hour": 15, "minute": 0} }

    2. To refresh the current vehicle status:
        {"command":"get_status"}

Note that not all functions are directly supported, i.e. not all functions are pre-processed; they are called as is, along with the 
single argument. In these cases, it is up to the user to ensure that the correctly formatted parameter is sent in the single arg (if at all possible).

"""
import inspect
import jlrpy
import configparser
import paho.mqtt.client as mqtt
import traceback
import json
import re
import datetime, time
import os,sys
import signal
from threading import Timer

LOG_LEVEL = jlrpy.logging.INFO

VERSION         = "0.8.3"
CONFIG_FILE     = "jlr2mqtt.cfg"

JLR_DEVICE_ID = "jlr2mqtt"
JLR_SYSTEM_SUBTOPIC = "system"
JLR_SYSTEM_SENSOR_TYPE = "system"
JLR_SYSTEM_TOPIC = "{}/{}".format(JLR_DEVICE_ID, JLR_SYSTEM_SUBTOPIC)

JLR_DEPARTURE_TIMERS_SENSOR_TYPE = "timers"
JLR_DEPARTURE_TIMERS_COUNT_MAX = 8

DEFAULT_COMMAND_STATUS_REFRESH_DELAY = 60
MAX_HA_DEPARTURE_TIMERS = 7     # openHAB mqtt binding HA discovery currently doesn't seem to support arrays; Manually add discovery timers to max defind here

def get_config_param(config,section,name,default):
    if config.has_option(section,name):
        return config.get(section,name)
    else:
        return default


config = configparser.RawConfigParser()
config.read(CONFIG_FILE)

MQTT_SERVER       = get_config_param(config,"MQTT", "MQTT_SERVER", "")                  
MQTT_SUB_TOPIC    = get_config_param(config,"MQTT", "MQTT_SUB_TOPIC", "").rstrip('/')               
MQTT_PUB_TOPIC    = get_config_param(config,"MQTT", "MQTT_PUB_TOPIC", "").rstrip('/')
MQTT_USER         = get_config_param(config,"MQTT", "MQTT_USER", "")
MQTT_PW           = get_config_param(config,"MQTT", "MQTT_PASSWORD", "")
MQTT_CLIENTID     = get_config_param(config,"MQTT", "MQTT_CLIENTID", "jlr2mqtt")
MQTT_QOS          = 0
MQTT_KEEPALIVE    = int(get_config_param(config,"MQTT", "MQTT_KEEPALIVE", "60"))
MQTT_RETAIN       = get_config_param(config,"MQTT", "MQTT_RETAIN", "false").lower() == "true"

JLR_USER = get_config_param(config,"JLR", "USER_ID", "")
JLR_PW = get_config_param(config,"JLR", "PASSWORD", "")
MASTER_PIN = get_config_param(config,"JLR", "PIN", None)

HOMEASSISTANT_DISCOVERY = get_config_param(config,"MISC", "HOMEASSISTANT_DISCOVERY", "false") == "true"
DISCOVERY_SENSORS_LIST =  get_config_param(config,"MISC", "DISCOVERY_SENSORS_LIST", "").replace("\n","").replace(" ","").replace("[","").replace("]","")

HA_TOPIC_BASE = "homeassistant"
HA_DEVICE_NAME_BASE = "Range Rover"

logger = jlrpy.logger
logger.level = LOG_LEVEL

jlr_connection = None
status_refresh_timer = None
ha_discovery_initalised = False
last_command_service_id = None


def sigterm_handler(_signo, _stack_frame):
    exit_gracefully()
    sys.exit(0)


def exit_gracefully():
    if mqtt_client:
        update_state_on_mqtt("offline")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


def initialise_mqtt_client(mqtt_client):
    #  print("init")
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PW)
    mqtt_client.on_connect = mqtt_on_connect
    mqtt_client.on_message = mqtt_on_message
    # mqtt_client.on_log = mqtt_on_log
    mqtt_client.on_disconnect = mqtt_on_disconnect

    logger.info("Connecting to mqtt server %s" % MQTT_SERVER)
    mqtt_client.connect(MQTT_SERVER, port=1883, keepalive=MQTT_KEEPALIVE, bind_address="")

    logger.info("Subscribing to mqtt topic '%s' for inbound commands" % MQTT_SUB_TOPIC)
    mqtt_client.subscribe(MQTT_SUB_TOPIC)

    return mqtt_client


def mqtt_on_connect(client, userdata, flags, rc):
    """ mqtt connection event processing """

    if rc == 0:
        client.connected_flag = True #set flag
        logger.info("MQTT connection established with broker")
        update_state_on_mqtt("online")
    else:
        logger.error("MQTT connection failed (code {})".format(rc))
        logger.debug(" mqtt userdata: {}, flags: {}, client: {}".format(userdata, flags, client))


def mqtt_on_disconnect(client, userdata, rc):
    """ mqtt disconnection event processing """
    update_state_on_mqtt("offline")
    client.loop_stop()
    if rc != 0:
        logger.warning("Unexpected disconnection.")
        logger.debug("[DEBUG] mqtt rc: {}, userdata: {}, client: {}".format(rc, userdata, client))


def mqtt_on_log(client, obj, level, string):
    """ mqtt log event received """
    logger.debug ("[DEBUG] MQTT log message received. Client: {}, obj: {}, level: {}".format(client, obj, level))
    logger.debug("[DEBUG] MQTT log msg: {}".format(string))


def mqtt_on_message(client, userdata, msg):
    """ mqtt message received on subscribed topic """
    
    logger.debug("Incoming message received: {}".format(msg.payload))
    try:
        payload = str(msg.payload, 'utf-8')
        json_data = json.loads(payload)
        if "command" in json_data:
            mqtt_client.publish("{}/send_command_ts".format(JLR_SYSTEM_TOPIC), get_timestamp_string(), 0, True)
            do_command(json_data)            
        else:
            mqtt_client.publish("{}/send_command_response_ts".format(JLR_SYSTEM_TOPIC),"", 0, True)
            logger.error("Command not recognised: {}".format(json_data))

    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))                
        logger.error("msg.payload: {} (payload str: '{}')".format(msg.payload, payload))
        msg = {"Status": "Error", "errorDescription": "{} (line {})".format(e, exc_tb.tb_lineno)}
        publish_command_response("{}".format(msg)) 
    

def update_state_on_mqtt(state):
    mqtt_client.publish("{}/state".format(MQTT_PUB_TOPIC), state, MQTT_QOS, True)
    mqtt_client.publish("{}/config".format(MQTT_PUB_TOPIC), '{"version":"' + VERSION + '"}', MQTT_QOS, True)
    mqtt_client.publish("{}/system_state_updated".format(MQTT_PUB_TOPIC),  get_timestamp_string(), MQTT_QOS, True)


def log_system_error(error, line_number=None):
    desc = "{} (line {})".format(error, line_number) if line_number else "{}".format(error) 
    logger.error(desc)
    msg = {"Status": "Error", "errorDescription": desc}    
    publish_command_response("{}".format(msg)) 


def get_category_from_key(key):
    name_parts = key.split("_")
    if len(name_parts) > 1:
        category = name_parts[0]
    else:
        category = None
    return category


def init_ha_discovery_for_dict(sensors_dict, sensor_type="status"):
    try:
        for sensor in sensors_dict:
            # Use the "key" value from the status dict item if available, otherwise fallback to:
            # (a) using the parent name, e.g. for alerts such as "preconditioning_started" with children "value", "active" and "lastUpdatedTime", or
            # (b) the object item itself if there is no further children, e.g. "longitude"
            
            key = sensor["key"] if "key" in sensor else sensor
            if len(DISCOVERY_SENSORS_LIST) == 0 or key.lower() in DISCOVERY_SENSORS_LIST or (sensor_type == "location" and 
                "position" in DISCOVERY_SENSORS_LIST): 

                category = get_category_from_key(key) 
                sensor_obj = sensor if sensor_type != "location" else sensors_dict[sensor]
                if sensor_obj:
                    for value_item in sensor_obj:
                        if value_item != "key":
                            topic, config = get_ha_disc_topic_and_config(key, value_item, category, sensor_type)  
                            mqtt_client.publish(topic, json.dumps(config), MQTT_QOS, True)
            else:
                logger.debug("HA_init: Dropping {}".format(key))
            
        logger.debug("HomeAssistant configuration topics for '{}' published".format(sensor_type))
    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))                


def init_ha_discovery_for_standard_items():
    """  'last_update_ts' timestamp and the 'send_command' topics are standard items  """
    topic, config = get_ha_disc_topic_and_config(JLR_SYSTEM_SUBTOPIC,"last_update_ts", None, JLR_SYSTEM_SENSOR_TYPE, False)
    mqtt_client.publish(topic, json.dumps(config), MQTT_QOS, True)
    topic, config = get_ha_disc_topic_and_config(JLR_SYSTEM_SUBTOPIC, "send_command", None, JLR_SYSTEM_SENSOR_TYPE, True)
    mqtt_client.publish(topic, json.dumps(config), MQTT_QOS, True)
    topic, config = get_ha_disc_topic_and_config(JLR_SYSTEM_SUBTOPIC, "send_command_response", None, JLR_SYSTEM_SENSOR_TYPE, False)
    mqtt_client.publish(topic, json.dumps(config), MQTT_QOS, True)
    topic, config = get_ha_disc_topic_and_config(JLR_SYSTEM_SUBTOPIC, "send_command_response_ts", None, JLR_SYSTEM_SENSOR_TYPE, False)
    mqtt_client.publish(topic, json.dumps(config), MQTT_QOS, True)
    topic, config = get_ha_disc_topic_and_config(JLR_SYSTEM_SUBTOPIC, "send_command_service_id", None, JLR_SYSTEM_SENSOR_TYPE, False)
    mqtt_client.publish(topic, json.dumps(config), MQTT_QOS, True)
    
    # Departure timers
    for count in range(JLR_DEPARTURE_TIMERS_COUNT_MAX):
        topic, config = get_ha_disc_topic_and_config("departure_timers", str(count), None, JLR_DEPARTURE_TIMERS_SENSOR_TYPE, False)
        mqtt_client.publish(topic, json.dumps(config), MQTT_QOS, True)


def get_ha_disc_topic_and_config(key, value_item, category, sensor_type="status", is_command=False):
    try:
        if category:
            state_topic = "jlr2mqtt/{}/{}/{}".format(sensor_type, category.upper(), key.lower())         
            device_identifier = "jlr_{}_{}".format(category, sensor_type)
            formatted_cat = category.upper() if len(category) <4 else category.title()
            formatted_cat += " Data" if sensor_type == "status" else " " + sensor_type.title()
            device_name = "{}: {}".format(HA_DEVICE_NAME_BASE, formatted_cat)
            sensor_name = ("{} {} - {}".format(category.upper() if len(category) < 4 else category.title(),
                sensor_type.capitalize(), key[len(category) + 1:].title())).replace("_", " ")        
            unique_id = "jlr_{}_{}".format(sensor_type.lower(), key.lower())   
            if sensor_type != "status":
                sensor_name = "{}: {}".format(sensor_name, value_item)
                unique_id = "{}_{}".format(unique_id, value_item.lower())
                state_topic = "{}/{}".format(state_topic, value_item.lower())
        else:
            if value_item:
                state_topic = "jlr2mqtt/{}/{}".format(key.lower(), value_item.lower()) 
                sensor_name = "{} {}: {}".format(sensor_type.replace("_", " ").title(), key.title().replace("_", " "), value_item)
                unique_id = "jlr_{}_{}".format(key.lower().replace(" ","_"), value_item.lower())   
            else:
                state_topic = "jlr2mqtt/{}".format(key.lower()) 
                sensor_name = "{}: {}".format(sensor_type.title().replace("_"," "), key)
                unique_id = "jlr_{}".format(key.lower().replace(" ","_"))   

            device_identifier = "jlr_{}".format(sensor_type)
            device_name = "{}: {} Data".format(HA_DEVICE_NAME_BASE, sensor_type.upper() if len(sensor_type) < 4 else sensor_type.title().replace("_"," "))

        if is_command:
            state_command_topic = "command_topic"
        else:
            state_command_topic = "state_topic"
            
        config = {
            # "value_template": "{{ value }}",
            state_command_topic: state_topic,
            "name": sensor_name,
            "unique_id": unique_id,
            "device": {"identifiers": [device_identifier],
                "name": device_name,
                "model": "Remote API Status: {}".format(key),
                "manufacturer": "Land Rover Jaguar"
            },
            "availability_topic": "jlr2mqtt/state",
            "payload_available": "online",
            "payload_not_available": "offline"        
        }
            
        if sensor_type == "status":
            config_parent_topic = key[len(category) + 1:].lower() if category else key.lower()
            device_topic = "{}_{}".format(sensor_type.lower(),category.lower()) if category else sensor_type.lower()
        else:
            config_parent_topic = value_item if value_item else key
            device_topic = "{}_{}".format(sensor_type.lower(), key.lower()) if sensor_type not in [JLR_SYSTEM_SENSOR_TYPE, "timers"] else sensor_type
        topic = "{}/sensor/jlr2mqtt_{}/{}/config".format(HA_TOPIC_BASE, device_topic, config_parent_topic)  

        return topic, config
    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))                
        return None, None

    
def get_jlr_connection():
    global jlr_connection    
    logger.debug("Checking for existing connection")
    if jlr_connection is not None:
        logger.debug("Connection found. Checking expiry")
        now_ts = int(datetime.datetime.now().timestamp())
        if now_ts > jlr_connection.expiration:
            jlr_connection = None
    else:
        logger.info("No existing 'jlr_connection' available")

    if jlr_connection is None:
        logger.info("Instantiating new connection to JLR")
        jlr_connection = jlrpy.Connection(JLR_USER, JLR_PW, JLR_DEVICE_ID)
    else:
        logger.debug("Connecton exists....")
    
    return jlr_connection


def get_departure_timers(v):
    departure_timers = v.get_departure_timers()
    if departure_timers and "departureTimerSetting" in departure_timers and "timers" in departure_timers["departureTimerSetting"]:
        return departure_timers["departureTimerSetting"]["timers"]
    else:
        return None


def get_timestamp_string():
    return datetime.datetime.now().strftime("%Y-%m-%dT%XZ")


def publish_command_response(response):
    mqtt_client.publish("{}/send_command_response".format(JLR_SYSTEM_TOPIC), "{}".format(response), 0, True)
    mqtt_client.publish("{}/send_command_response_ts".format(JLR_SYSTEM_TOPIC), get_timestamp_string(), 0, True)
    if response and "customerServiceId" in response and response["customerServiceId"]:
        mqtt_client.publish("{}/send_command_service_id".format(JLR_SYSTEM_TOPIC), "{}".format(response["customerServiceId"]), 0, True)
    else:
        mqtt_client.publish("{}/send_command_service_id".format(JLR_SYSTEM_TOPIC), "", 0, True)


def publish_status_dict(status_dict, subtopic, key="key"):
    for element in status_dict:        
        category = element[key].split("_")[0]
        topic_base = "{}/{}/{}/{}".format(MQTT_PUB_TOPIC, subtopic, category, element[key].lower())

        if len(element) == 2 and key in element and "value" in element:
            mqtt_client.publish(topic_base, element["value"],MQTT_QOS, MQTT_RETAIN)
        else:
            for prop in element:
                if prop != key:
                    topic = "{}/{}".format(topic_base, prop)
                    mqtt_client.publish(topic, element[prop], MQTT_QOS, MQTT_RETAIN)
    
    mqtt_client.publish("{}/last_update_ts".format(JLR_SYSTEM_TOPIC), get_timestamp_string(), MQTT_QOS, MQTT_RETAIN)


def publish_departure_timers(timers):
    # First clear any existing timers - assume max index of 10 for now
    for i in range(0,10):
        topic = "{}/departure_timers/{}".format(MQTT_PUB_TOPIC, i)
        # logger.info("[DEBUG] posting empty string to topic '{}'".format(topic)) 
        mqtt_client.publish(topic, "", MQTT_RETAIN)    
    
    # Now do actual timers
    if timers:
        for timer in timers:
            key = timer["timerIndex"]
            topic = "{}/departure_timers/{}".format(MQTT_PUB_TOPIC, key)
            mqtt_client.publish(topic, json.dumps(timer), MQTT_RETAIN)    
            logger.debug("publish_departure_timers: Publishing to '{}': '{}'".format(topic,json.dumps(timer)))
    else:
        logger.debug("publish_departure_timers: No timers found")
        topic = "{}/departure_timers".format(MQTT_PUB_TOPIC)
        mqtt_client.publish(topic, "[]" , MQTT_RETAIN)    
    
    
def publish_position(location):
    position = location["position"]
    if "longitude" in position and "latitude" in position:
        position["latlong"] = "{}, {}".format(position["latitude"], position["longitude"])
    else:
        logger.info("[DEBUG] long/lat not found... {}".format(location))
    for loc_element in position:
        topic = "{}/position/{}".format(MQTT_PUB_TOPIC, loc_element)
        mqtt_client.publish(topic, position[loc_element], MQTT_QOS, True)    # Retain last position data...


def get_and_publish_reverse_geocode(loc_json):
    if loc_json and "longitude" in loc_json and "latitude" in loc_json:
        ret = jlr_connection.reverse_geocode(loc_json["latitude"], loc_json["longitude"])
        logger.debug("reverse_geocode result: {}".format(ret))
        if "formattedAddress" in ret:
            # Note that the formatted address topic is posted to the parent 'position' topic as well as the 'address' subtopic. This is to comply with
            # HomeAssitant discovery topic naming convention. Another option would be to put all the 'address' values into the main 'position' topic, instead of having
            # a child 'address' subtopic. Something to be considered later. TODO!!
            mqtt_client.publish("{}/position/formatted_address".format(MQTT_PUB_TOPIC), ret["formattedAddress"], MQTT_QOS, True)    
            for address_element in ret:
                topic = "{}/position/address/{}".format(MQTT_PUB_TOPIC, address_element)
                mqtt_client.publish(topic, ret[address_element], MQTT_QOS, True)    
            logger.debug("Address for location is: {}".format(ret["formattedAddress"]))
            return ret
        else:
            logger.info("Reverse geocode failed with ret={}".format(ret))
            return None
    else:
        logger.error("JSON string with longitude and latitude required. arg='{}'".format(loc_json))
        return None


def get_status(for_key=None):        
    if jlr_connection:        
        try:            
            v = jlr_connection.vehicles[0]
            global ha_discovery_initalised
            count = 4 if ha_discovery_initalised else 5
            if not for_key:
                logger.info("[+] Getting complete status data from JLR")
                full_status = v.get_status()
                alerts = full_status["vehicleAlerts"] if "vehicleAlerts" in full_status else {}
                status = full_status["vehicleStatus"] if "vehicleStatus" in full_status else {}
                location = v.get_position()
                publish_status_dict(status, "status")
                logger.info("1/{} Status data published to mqtt".format(count))
                
                publish_status_dict(alerts, "alerts")
                logger.info("2/{} Alerts information published to mqtt".format(count))
                
                if "position" in location:
                    publish_position(location)
                    if "longitude" in location["position"] and "latitude" in location["position"]:
                        get_and_publish_reverse_geocode(location["position"])                        
                logger.info("3/{} Location data published to mqtt".format(count))
                
                timers = get_departure_timers(v)
                publish_departure_timers(timers)
                if timers:
                    logger.info("4/{} '{}' departure timer(s) published to mqtt".format(count, len(timers)))
                else:
                    logger.info("4/{} No departure timers found".format(count))
                
                if HOMEASSISTANT_DISCOVERY and not ha_discovery_initalised:
                    init_ha_discovery_for_dict(status, "status")
                    
                    if "alerts_" in DISCOVERY_SENSORS_LIST:
                        init_ha_discovery_for_dict(alerts, "alerts")
                    if "position" in DISCOVERY_SENSORS_LIST:
                        init_ha_discovery_for_dict(location,"location")                        
                        topic, config = get_ha_disc_topic_and_config("position","formatted_address", None, "location", False)
                        # print ("Formatted add topic/conf: {} \n\n{}".format(topic, config))
                        mqtt_client.publish(topic, json.dumps(config), MQTT_QOS, True)
                            
                    init_ha_discovery_for_standard_items()
                    ha_discovery_initalised = True
                    logger.info("5/{} HomeAssistant auto discovery topics published".format(count))
                  
            else:
                logger.info("[+] Updating status for '{}'".format(for_key))
                status = v.get_status(for_key.upper())
                cat = for_key.split("_")[0]
                topic = "{}/status/{}/{}/value".format(MQTT_PUB_TOPIC, cat, for_key.lower())
                mqtt_client.publish(topic, status, MQTT_QOS, True)
                logger.info("1/1 {}: {}".format(for_key, status))
            
            return {"status": "Completed"}

        except Exception as e:
            _, _, exc_tb = sys.exc_info()
            logger.error("{} (line {})".format(e, exc_tb.tb_lineno))                
            return {"status": "Error", "errorDescription": "{} (line {})".format(e, exc_tb.tb_lineno)}
        
    else:        
        logger.error("'get_status({})' failed as no connection available".format(for_key))
        return {"status": "Error", "errorDescription": "get_status({}) failed as no connection available".format(for_key)}


def do_command(json_data):   
    global last_command_service_id

    # Clear out any historical response data
    publish_command_response({"status": ""}) 
    try:
        get_jlr_connection()   
        if jlr_connection:
            v = jlr_connection.vehicles[0]

            ret = None
            command = json_data["command"]
            status_refresh_delay = DEFAULT_COMMAND_STATUS_REFRESH_DELAY

            # First check if we have a 'custom' command...
            if "set_log_level" == command:
                if "level" in json_data:
                    level = json_data["level"].upper()
                    logger.level = getattr(jlrpy.logging, level)
                    ret={"status": "Completed"}
                else:
                    ret={"status": "Error: Log level not key 'level' not found"}
                status_refresh_delay = -1
            elif "init_ha_discovery" == command:
                # Reset initialised status
                global ha_discovery_initalised
                ha_discovery_initalised = False
                # refresh the sensors list 
                global DISCOVERY_SENSORS_LIST 
                DISCOVERY_SENSORS_LIST =  get_config_param(config,"MISC", "DISCOVERY_SENSORS_LIST", "").replace("\n","").replace(" ","").replace("[","").replace("]","")
                ret = get_status()
                status_refresh_delay = -1
            elif "get_status" == command:
                for_key = json_data["key"] if "key" in json_data else None
                ret = get_status(for_key)
                status_refresh_delay = -1
            elif "refresh_last_command_status" == command:
                logger.debug("processing 'refresh_last_command_status'")
                if last_command_service_id:
                    logger.debug("in if.... {}".format(last_command_service_id))
                    do_command({"command": "get_service_status", "kwargs" : {"service_id" : last_command_service_id}})
                    status_refresh_delay = -1
                    return
            else:
                # It's not an 'internal' command, so send it to the API...
                try:
                    command_func = getattr(v, command)
                    func_params = sorted(list(inspect.signature(command_func).parameters.keys()))
                    kwargs = json_data["kwargs"] if "kwargs" in json_data else {}

                    # Use PIN from config file if defined
                    if "pin" in func_params and MASTER_PIN and "pin" not in kwargs:
                        kwargs["pin"] = MASTER_PIN

                    given_params = sorted(list(kwargs))                    
                    if func_params and given_params and type(kwargs) is dict:
                        params_ok = (given_params == func_params)
                        if not params_ok:
                            ret = "Parameter(s) missing for '{}'. Given param(s): '{}'; Required: '{}'".format(
                                command, ", ".join(given_params),", ".join(func_params))
                    elif func_params and not kwargs:
                        ret = ("'kwargs' dict with function parameters missing in json, for function '{}({})'".format(command, ",".join(func_params)))
                        params_ok = False
                    else: # func_params is False/zero length, so no need for params
                        params_ok = True
                    
                    if not params_ok:                    
                        raise Exception(ret)
                                        
                    logger.debug("Calling command function '{}' with parameters {}".format(command, kwargs) if given_params else "Calling function '{}'".format(command))
                    ret = command_func(**kwargs)
                except Exception as e:
                    _, _, exc_tb = sys.exc_info()
                    logger.error("{} (line {})".format(e, exc_tb.tb_lineno))                
                    ret = {"status" : "Error: {} (line {})]".format(e, exc_tb.tb_lineno)}
            
            publish_command_response(ret)

            last_command_service_id = ret["customerServiceId"] if ret and "customerServiceId" in ret and ret["customerServiceId"] else None

            if ret and "status" in ret and not "Error" in ret["status"]:
                refresh_notice = ". Status will be refreshed in {} seconds".format(status_refresh_delay) if status_refresh_delay > 0 else ""
                logger.info("'{}' command completed{}".format(command, refresh_notice))  
                logger.debug("'{}' return value: {}".format(command, ret))
                if status_refresh_delay > 0 and command != "get_service_status":
                    global status_refresh_timer
                    if status_refresh_timer is not None and status_refresh_timer.is_alive():
                        status_refresh_timer.cancel()
                        status_refresh_timer = None
                    status_refresh_timer = Timer(status_refresh_delay, get_status)
                    status_refresh_timer.start()
            else:
                logger.warn("'{}' failed. ret={}".format(command, ret))
        else:
            logger.error("'{}' command failed as connection to JLR is unavailable".format(command))    
            publish_command_response({"status":"Error: '{}' command failed as connection to JLR is unavailable".format(command)})
    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} @line number {}. json_data = '{}'".format(e, exc_tb.tb_lineno, json_data))                
        
    


signal.signal(signal.SIGTERM, sigterm_handler)
if not MQTT_SERVER:
    logger.critical("Unable to start system as MQTT broker not defined")
    sys.exit(0)
    
try:
    mqtt_client = initialise_mqtt_client(mqtt.Client(client_id=MQTT_CLIENTID))
    mqtt_client.loop_forever()

except KeyboardInterrupt:
    exit_gracefully()

logger.info("Session ended")
