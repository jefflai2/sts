# Copyright 2011-2013 Colin Scott
# Copyright 2011-2013 Andreas Wundsam
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''
Encapsulates the state of the simulation, including:
  - The controllers
  - The topology
  - Dataplane forwarding
  - (Optionally) the dataplane trace
  - Metadata (e.g. # of failures)
'''

from sts.util.io_master import IOMaster
from sts.dataplane_traces.trace import Trace
from entities import DeferredOFConnection
from sts.controller_manager import ControllerManager, UserSpaceControllerPatchPanel
from sts.util.deferred_io import DeferredIOWorker
from sts.openflow_buffer import OpenFlowBuffer
from sts.topology import *
from sts.invariant_checker import ViolationTracker
from sts.syncproto.sts_syncer import STSSyncConnectionManager
import sts.snapshot as snapshot
from sts.util.socket_mux.base import MultiplexedSelect
from sts.util.socket_mux.sts_socket_multiplexer import STSSocketDemultiplexer, STSMockSocket
from pox.lib.util import connect_socket_with_backoff

import select
import socket
import logging
import time

log = logging.getLogger("simulation")

def default_boot_controllers(controller_configs, snapshot_service, sync_connection_manager):
  # Boot the controllers
  controllers = []
  for c in controller_configs:
    controller = c.controller_class(c, sync_connection_manager, snapshot_service)
    controller.start()
    log.info("Launched controller %s: %s [PID %d]" %
             (str(c.cid), " ".join(c.expanded_start_cmd), controller.pid))
    controllers.append(controller)
  return ControllerManager(controllers)

class SimulationConfig(object):
  """
  Maintains the configuration for:
    - The controllers: a list of ControllerConfig objects
    - The topology
    - Patch panel (dataplane forwarding)
    - (Optionally) the dataplane trace
    - Initialization parameters (switch_init_sleep_seconds)
  """
  def __init__(self, controller_configs=None,
               topology_class=FatTree,
               topology_params="",
               patch_panel_class=BufferedPatchPanel,
               controller_patch_panel_class=UserSpaceControllerPatchPanel,
               dataplane_trace=None,
               snapshot_service=None,
               multiplex_sockets=False,
               violation_persistence_threshold=None,
               kill_controllers_on_exit=True,
               interpose_on_controllers=False):
    '''
    Constructor parameters:
      topology_class    => a sts.topology.Topology class (not object!)
                           defining the switches and links
      topology_params   => Comma-delimited list of arguments to pass into the FatTree
                           constructor, specified just as you would type them within
                           the parens.
      patch_panel_class => a sts.topology.PatchPanel class (not object!)
      dataplane_trace   => a path to a dataplane trace file
                           (e.g. dataplane_traces/ping_pong_same_subnet.trace)
      violation_persistence_threshold => number of logical time units to observe a
                                         violation before we declare that it is
                                         persistent
      switch_init_sleep_seconds => number of seconds to wait for switches to
                                   connect to controllers before starting the
                                   simulation. Defaults to False (no wait).
      monkey_patch_select => whether to use STS's custom deterministic
                             select. Requires that the controller is
                             monkey-patched too
    '''
    if controller_configs is None:
      controller_configs = []
    self.controller_configs = controller_configs
    # keep around topology_class and topology_params so we can construct
    # clean topology objects for (multiple invocations of) bootstrapping later
    self._topology_class = topology_class
    self._topology_params = topology_params
    self._patch_panel_class = patch_panel_class
    self._dataplane_trace_path = dataplane_trace
    self._violation_persistence_threshold = violation_persistence_threshold
    self._kill_controllers_on_exit = kill_controllers_on_exit

    # TODO(cs): is the snapshot service stateful?
    if snapshot_service is None:
      # For snapshotting the controller's view of the network configuration
      snapshot_service = snapshot.get_snapshotservice(controller_configs)
    self.snapshot_service = snapshot_service
    self.current_simulation = None
    self.multiplex_sockets = multiplex_sockets
    self.interpose_on_controllers = interpose_on_controllers
    self.controller_patch_panel_class = controller_patch_panel_class

  def bootstrap(self, sync_callback, boot_controllers=default_boot_controllers):
    '''Return a simulation object encapsulating the state of
       the system in its initial starting point:
       - boots controllers
       - connects switches to controllers

       May be invoked multiple times!
    '''
    def remove_monkey_patch():
      if hasattr(select, "_old_select"):
        # Revert the previous monkeypatch to allow the new true_sockets to
        # connect
        select.select = select._old_select
        socket.socket = socket._old_socket

    def initialize_io_loop():
      ''' boot the IOLoop (needed for the controllers) '''
      _io_master = IOMaster()
      # monkey patch time.sleep for all our friends
      _io_master.monkey_time_sleep()
      # tell sts.console to use our io_master
      msg.set_io_master(_io_master)
      return _io_master

    def wire_controller_patch_panel(controller_manager, create_io_worker):
      patch_panel = None
      if not self.interpose_on_controllers:
        return patch_panel
      # N.B. includes local controllers in network namespaces or VMs.
      remote_controllers = controller_manager.remote_controllers
      if len(remote_controllers) != 0:
        patch_panel = self.controller_patch_panel_class(create_io_worker)
        for c in remote_controllers:
          patch_panel.register_controller(c.cid, c.guest_eth_addr, c.host_device)
      return patch_panel

    def instantiate_topology(create_io_worker):
      '''construct a clean topology object from topology_class and
      topology_params'''
      log.info("Creating topology...")
      # If you want to shoot yourself in the foot, feel free :)
      comma = "" if self._topology_params == "" else ","
      topology = eval("%s(%s%screate_io_worker=create_io_worker)" %
                      (self._topology_class.__name__,
                       self._topology_params, comma))
      return topology

    # Instantiate the pieces needed for Simulation's constructor
    remove_monkey_patch()
    io_master = initialize_io_loop()
    sync_connection_manager = STSSyncConnectionManager(io_master,
                                                       sync_callback)
    controller_manager = boot_controllers(self.controller_configs,
                                          self.snapshot_service,
                                          sync_connection_manager)
    controller_patch_panel = wire_controller_patch_panel(controller_manager,
                                                         io_master.create_worker_for_socket)
    topology = instantiate_topology(io_master.create_worker_for_socket)
    patch_panel = self._patch_panel_class(topology.switches, topology.hosts,
                                          topology.get_connected_port)
    openflow_buffer = OpenFlowBuffer()
    dataplane_trace = None
    if self._dataplane_trace_path is not None:
      dataplane_trace = Trace(self._dataplane_trace_path, topology)
    if self._violation_persistence_threshold is not None:
      violation_tracker = ViolationTracker(self._violation_persistence_threshold)
    else:
      violation_tracker = ViolationTracker()

    simulation = Simulation(topology, controller_manager, dataplane_trace,
                            openflow_buffer, io_master, controller_patch_panel,
                            patch_panel, sync_callback, self.multiplex_sockets,
                            violation_tracker, self._kill_controllers_on_exit)
    self.current_simulation = simulation
    return simulation

  def set_dataplane_trace_path(self, path):
    if self._dataplane_trace_path is None:
      self._dataplane_trace_path = path

  def __str__(self):
    return ('''SimulationConfig(controller_configs=%s,\n'''
            '''                 topology_class=%s,\n'''
            '''                 topology_params="%s",\n'''
            '''                 patch_panel_class=%s,\n'''
            '''                 multiplex_sockets=%s,\n'''
            '''                 kill_controllers_on_exit=%s)''' %
            (str(self.controller_configs),self._topology_class.__name__,
             self._topology_params, self._patch_panel_class.__name__,
             str(self.multiplex_sockets), str(self._kill_controllers_on_exit)))

class Simulation(object):
  '''
  Encapsulates the running state of a single simulation:
    - Topology (network state)
    - Controller processes
    - OpenFlowBuffer (OpenFlow messages)
    - PatchPanel (Dataplane messages)
    - RecordingSyncCallback (controller state changes)
    - Dataplane Trace (pending dataplane messages)
  '''
  def __init__(self, topology, controller_manager, dataplane_trace,
               openflow_buffer, io_master, controller_patch_panel, patch_panel,
               controller_sync_callback, multiplex_sockets,
               violation_tracker, kill_controllers_on_exit):
    self.topology = topology
    self.controller_manager = controller_manager
    self.controller_manager.set_simulation(self)
    self.dataplane_trace = dataplane_trace
    self.openflow_buffer = openflow_buffer
    self._io_master = io_master
    self.controller_patch_panel = controller_patch_panel
    self.patch_panel = patch_panel
    self.controller_sync_callback = controller_sync_callback
    self.multiplex_sockets = multiplex_sockets
    self.violation_tracker = violation_tracker
    self._kill_controllers_on_exit = kill_controllers_on_exit
    self.exit_code = 0

  def set_exit_code(self, code):
    self.exit_code = code

  def set_pass_through(self):
    ''' Set to pass-through during bootstrap, so that switch initialization
    messages don't get buffered '''
    self.openflow_buffer.set_pass_through()
    if hasattr(self.controller_sync_callback, "set_pass_through"):
      self.controller_sync_callback.set_pass_through()

  def unset_pass_through(self):
    ''' unset pass-through mode '''
    observed_events = []
    observed_events += self.openflow_buffer.unset_pass_through()
    if hasattr(self.controller_sync_callback, "unset_pass_through"):
      observed_events += self.controller_sync_callback.unset_pass_through()
    return observed_events

  def clean_up(self):
    '''Ensure that state from previous runs (old controller processes,
    sockets, IOLoop object) are cleaned before the next time we
    bootstrap'''
    # kill controllers
    if self.controller_manager is not None and self._kill_controllers_on_exit:
      self.controller_manager.kill_all()

    # Garbage collect sockets
    if self.topology is not None:
      for switch in self.topology.switches:
        for connection in switch.connections:
          connection.close()

    if self.controller_patch_panel is not None:
      self.controller_patch_panel.clean_up()

    # Just to make sure there isn't any state lying around, throw out
    # the old RecocoIOLoop
    msg.unset_io_master()
    if self._io_master is not None:
      self._io_master.close_all()

  @property
  def io_master(self):
    return self._io_master

  def connect_to_controllers(self):
    ''' Connect all switches to all controllers '''
    def revert_socket_monkeypatch():
      if hasattr(socket, "_old_socket"):
        socket.socket = socket._old_socket

    def revert_select_monkeypatch():
      if hasattr(select, "_old_select"):
        select.select = select._old_select

    def monkeypatch_select():
      log.debug("Monkeypatching STS select")
      mux_select = None
      demuxers = []
      if self.multiplex_sockets:
        revert_select_monkeypatch()
        revert_socket_monkeypatch()
        # Monkey patch select to use our deterministic version
        mux_select = MultiplexedSelect()
        for c in self.controller_manager.controller_configs:
          # Connect the true sockets
          true_socket = connect_socket_with_backoff(address=c.address, port=c.port)
          true_socket.setblocking(0)
          io_worker = mux_select.create_worker_for_socket(true_socket)
          mux_select.set_true_io_worker(io_worker)
          demux = STSSocketDemultiplexer(io_worker, c.server_info)
          demuxers.append(demux)

        # Monkey patch select.select
        select._old_select = select.select
        select.select = mux_select.select
        # Monkey patch socket.socket
        socket._old_socket = socket.socket
        def socket_patch(protocol, sock_type):
          if sock_type == socket.SOCK_STREAM:
            return STSMockSocket(protocol, sock_type)
          else:
            socket._old_socket(protocol, sock_type)
        socket.socket = socket_patch

      return (mux_select, demuxers)

    def create_connection(controller_info, switch, max_backoff_seconds=1024):
      ''' Connect switches to controllers. May raise a TimeoutError '''
      while controller_info.address == "__address__":
        log.debug("Waiting for controller address for %s..." % controller_info.label)
        time.sleep(5)
      # TODO(cs): move this into a ConnectionFactory class
      socket = connect_socket_with_backoff(controller_info.address,
                                           controller_info.port,
                                           max_backoff_seconds=max_backoff_seconds)
      # Set non-blocking
      socket.setblocking(0)
      io_worker = DeferredIOWorker(self.io_master.create_worker_for_socket(socket))
      connection = DeferredOFConnection(io_worker, controller_info.cid, switch.dpid, self.openflow_buffer)
      return connection

    (self.mux_select, self.demuxers) = monkeypatch_select()

    self.topology.connect_to_controllers(self.controller_manager.controller_configs,
                                         create_connection=create_connection)

    # create_connection should not be called again --revert monkeypatch in
    # case STS wants to open other sockets (e.g., xmlrplclib)
    revert_socket_monkeypatch()
