import collections
import itertools
import json
import time

from sts.io_master import IOMaster
from sts.syncproto.base import SyncProtocolSpeaker, SyncMessage, SyncTime

from pox.lib.ioworker.io_worker import JSONIOWorker
from pox.lib.util import parse_openflow_uri, connect_socket_with_backoff

class STSSyncProtocolSpeaker(SyncProtocolSpeaker):
  def __init__(self, controller, state_master, json_io_worker=None):
    self.state_master = state_master
    self.controller = controller

    handlers = {
        ("ASYNC", "StateChange"): self._log_state_change,
        ("REQUEST", "DeterministicValue"): self._get_deterministic_value
    }
    SyncProtocolSpeaker.__init__(self, handlers, json_io_worker)

  def _log_state_change(self, message):
    self.state_master.state_change(self.controller, message.time, message.fingerPrint, message.name, message.value)

  def _get_deterministic_value(self, message):
    value = self.state_master.get_deterministic_value(self.controller, message.name)
    response = SyncMessage(type="RESPONSE", messageClass="DeterministicValue", time=SyncTime.now(), xid = message.xid, value=value)
    self.send(response)

class STSSyncConnection(object):
  """ A connection to a controller with the sts sync protocol """
  def __init__(self, controller, state_master, sync_uri):
    self.controller = controller
    (self.mode, self.host, self.port) = parse_openflow_uri(sync_uri)
    self._on_disconnect = []
    self.speaker = STSSyncProtocolSpeaker(controller=controller, state_master=state_master)
    self.io_worker = None

  def on_disconnect(self, func):
    self._on_disconnect.append(func)

  def connect(self, io_master):
    if self.mode != "tcp":
      raise RuntimeError("only tcp (active) mode supported by now")

    socket = connect_socket_with_backoff(self.host, self.port)
    self.io_worker = JSONIOWorker(io_master.create_worker_for_socket(socket))
    self.speaker.io_worker = self.io_worker

  def disconnect(self):
    if(self.io_worker):
      self.io_worker.close()
    for handler in self._on_disconnect:
      handler(self)

  def close(self):
    self.disconnect()

class STSSyncConnectionManager(object):
  """the connection manager for the STS sync protocols. 
     TODO: finish"""
  def __init__(self, io_master, state_master):
    self.io_master  = io_master
    self.sync_connections = []
    self.state_master = state_master

  def connect(self, controller, sync_uri):
    s = STSSyncConnection(controller=controller, state_master=self.state_master, sync_uri=sync_uri)
    s.connect(self.io_master)
    s.on_disconnect(self.remove_connection)

    self.sync_connections.append(s)

    return s

  def remove_connection(self, connection):
    if connection in self.sync_connections:
      self.sync_connections.remove(connection)
