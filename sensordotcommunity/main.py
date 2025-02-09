"""
A Hue plugin, for controlling lights connected to your Hue Bridge
"""

import six
import logging
import time
import requests
import simplejson as json
from threading import Thread, Lock
from six.moves.queue import Queue, Empty
from plugins.base import om_expose, output_status, OMPluginBase, PluginConfigChecker, background_task, PluginWebResponse
from .plugin_logs import PluginLogHandler

if False:  # MYPY
    from typing import Dict, List, Optional, Callable

logger = logging.getLogger('openmotics')


class SensorDotCommunity(OMPluginBase):

    name = 'SensorDotCommunity'
    version = '1.0.1'
    interfaces = [('config', '1.0')]

    config_description = []
    default_config = []

    def __init__(self, webinterface, gateway_logger):
        self.setup_logging(log_function=gateway_logger)
        super(SensorDotCommunity, self).__init__(webinterface, logger)
        logger.info('Starting %s plugin %s ...', self.name, self.version)

        self._config = self.default_config
        self._config_checker = PluginConfigChecker(SensorDotCommunity.config_description)

        logger.info("%s plugin started", self.name)

    @staticmethod
    def setup_logging(log_function):  # type: (Callable) -> None
        logger.setLevel(logging.INFO)
        log_handler = PluginLogHandler(log_function=log_function)
        # some elements like time and name are added by the plugin runtime already
        # formatter = logging.Formatter('%(asctime)s - %(name)s - %(threadName)s - %(levelname)s - %(message)s')
        formatter = logging.Formatter('%(threadName)s - %(levelname)s - %(message)s')
        log_handler.setFormatter(formatter)
        logger.addHandler(log_handler)

    @om_expose(version=2, auth=False)
    def api(self, plugin_web_request):
        """
{
  "esp8266id": "12345678",
  "software_version": "NRZ-2020-133",
  "sensordatavalues": [
    {
      "value_type": "SDS_P1",
      "value": "7.18"
    },
    {
      "value_type": "SDS_P2",
      "value": "2.58"
    },
    {
      "value_type": "temperature",
      "value": "23.20"
    },
    {
      "value_type": "humidity",
      "value": "46.40"
    },
    {
      "value_type": "samples",
      "value": "5029246"
    },
    {
      "value_type": "min_micro",
      "value": "28"
    },
    {
      "value_type": "max_micro",
      "value": "20132"
    },
    {
      "value_type": "interval",
      "value": "145000"
    },
    {
      "value_type": "signal",
      "value": "-60"
    }
  ]
}        """
        method = plugin_web_request.method
        path = plugin_web_request.path
        body = plugin_web_request.body
        params = plugin_web_request.params
        headers = plugin_web_request.headers
        errors = []

        logger.debug('%s %s %s', method, path, params)
        logger.debug('%s', headers)
        logger.debug('%s', body)

        data = json.loads(body)
        known_sensors = self._get_known_sensors()

        device_id = data["esp8266id"]
        for entry in data.get("sensordatavalues", []):
            value_type = entry["value_type"]
            value = float(entry["value"])
            sensor_external_id = "{}/{}".format(device_id, value_type)
            if value_type == "temperature":
                name = "Temperature"
                physical_quantity = "temperature"
                unit = "celcius"
            elif value_type == "humidity":
                name = "Humidity"
                physical_quantity = "humidity"
                unit = "percent"
            elif value_type == "SDS_P1":
                name = "PM10"
                physical_quantity = "dust"
                unit = "micro_gram_per_cubic_meter"
            elif value_type == "SDS_P2":
                name = "PM2.5"
                physical_quantity = "dust"
                unit = "micro_gram_per_cubic_meter"
            else:
                logger.debug('unsupported sensor value %s', value_type)
                continue
            if sensor_external_id not in known_sensors.keys():
                logger.info('Registering new sensor %s with external id %s', name, sensor_external_id)
                om_sensor_id = self._register_sensor(name, sensor_external_id, physical_quantity, unit)
            else:
                om_sensor_id = known_sensors[sensor_external_id]
            if om_sensor_id is not None:
                logger.info('Updating sensor %s (%s) with %s (%s)', name, om_sensor_id, value, unit)
                self._update_sensor(om_sensor_id, value)
            else:
                msg = 'Sensor.community sensor {} ({}) not found'.format(name, sensor_external_id)
                logger.error(msg)
                errors.append(msg)
        if errors:
            return PluginWebResponse(status_code=500, body='\n'.join(errors), path=plugin_web_request.path)
        else:
            return PluginWebResponse(status_code=200, body='success', path=plugin_web_request.path)

    def _get_known_sensors(self):
        response = self.webinterface.get_sensor_configurations()
        data = json.loads(response)
        return {x['external_id']: x['id'] for x in data['config'] if x.get('source', {}).get('name') == SensorDotCommunity.name and x['external_id'] not in [None, '']}

    @om_expose
    def get_config_description(self):
        return json.dumps(SensorDotCommunity.config_description)

    @om_expose
    def get_config(self):
        return json.dumps(self._config)

    @om_expose
    def set_config(self, config):
        config = json.loads(config)
        for key in config:
            if isinstance(config[key], six.string_types):
                config[key] = str(config[key])
        self._config_checker.check_config(config)
        self._config = config
        self.write_config(config)
        return json.dumps({'success': True})

    def _register_sensor(self, name, external_id, physical_quantity, unit):
        logger.info('Registering sensor with name %s and external_id %s', name, external_id)
        data = {
            'external_id': external_id,
            'source': {'type': 'plugin', 'name': SensorDotCommunity.name},
            'name': name,
            'physical_quantity': physical_quantity,
            'unit': unit,
        }
        response = self.webinterface.set_sensor_configuration(config=json.dumps(data))
        data = json.loads(response)
        if data is None or not data.get('success', False):
            logger.error('Could not register new sensor, registration failed trough API')
            logger.error(data)
            return None
        response = self.webinterface.get_sensor_configurations()
        data = json.loads(response)
        sensor_id = next((x['id'] for x in data['config'] if x.get('external_id') == external_id and x.get('source', {}).get('name') == SensorDotCommunity.name), None)
        logger.info('Registered new sensor with name %s and external_id %s', name, external_id)
        return sensor_id

    def _update_sensor(self, sensor_id, value):
        logger.debug('Updating sensor %s with status %s', sensor_id, value)
        data = {'id': sensor_id, 'value': value}
        response = self.webinterface.set_sensor_status(status=json.dumps(data))
        data = json.loads(response)
        if data is None or not data.get('success', False):
            logger.warning('Could not set the updated sensor value')
