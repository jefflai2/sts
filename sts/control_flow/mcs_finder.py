# Copyright 2011-2013 Colin Scott
# Copyright 2011-2013 Andreas Wundsam
# Copyright 2012-2013 Andrew Or
# Copyright 2012-2013 Sam Whitlock
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
An orchestrating control flow that invokes replayer several times to
find the minimal causal sequence (MCS) of a failure.
'''

from sts.util.console import msg, color, Tee
from sts.util.convenience import timestamp_string, ExitCode, create_clean_python_dir
from sts.util.rpc_forker import LocalForker, test_serialize_response
from sts.util.precompute_cache import PrecomputeCache
from sts.replay_event import *
from sts.event_dag import EventDag, split_list
import sts.input_traces.log_parser as log_parser
from sts.input_traces.input_logger import InputLogger
from sts.control_flow.base import ControlFlow
from sts.control_flow.replayer import Replayer
from sts.control_flow.peeker import Peeker
from config.invariant_checks import name_to_invariant_check

from collections import Counter
import copy
import sys
import time
import random
import logging
import json
import os
import re

class MCSFinder(ControlFlow):
  def __init__(self, simulation_cfg, superlog_path_or_dag,
               invariant_check_name=None,
               transform_dag=None, end_wait_seconds=0.5,
               mcs_trace_path=None, extra_log=None, runtime_stats_path=None,
               wait_on_deterministic_values=False,
               no_violation_verification_runs=1,
               optimized_filtering=False, forker=LocalForker(),
               replay_final_trace=True, strict_assertion_checking=False,
               delay_flow_mods=False,
               **kwargs):
    super(MCSFinder, self).__init__(simulation_cfg)
    # number of subsequences delta debugging has examined so far, for
    # distingushing runtime stats from different intermediate runs.
    self.subsequence_id = 0
    self.mcs_log_tracker = None
    self.replay_log_tracker = None
    self.mcs_trace_path = mcs_trace_path
    self.sync_callback = None
    self._log = logging.getLogger("mcs_finder")

    if invariant_check_name is None:
      raise ValueError("Must specify invariant check")
    if invariant_check_name not in name_to_invariant_check:
      raise ValueError('''Unknown invariant check %s.\n'''
                       '''Invariant check name must be defined in config.invariant_checks''',
                       invariant_check_name)
    self.invariant_check = name_to_invariant_check[invariant_check_name]

    if type(superlog_path_or_dag) == str:
      self.superlog_path = superlog_path_or_dag
      # The dag is codefied as a list, where each element has
      # a list of its dependents
      self.dag = EventDag(log_parser.parse_path(self.superlog_path))
    else:
      self.dag = superlog_path_or_dag

    last_invariant_violation = self.dag.get_last_invariant_violation()
    if last_invariant_violation is None:
      raise ValueError("No invariant violation found in dag...")
    violations = last_invariant_violation.violations
    if len(violations) > 1:
      self.bug_signature = None
      while self.bug_signature is None:
        msg.interactive("\n------------------------------------------\n")
        msg.interactive("Multiple violations detected! Choose one for MCS Finding:")
        for i, violation in enumerate(violations):
          msg.interactive("  [%d] %s" % (i+1, violation))
        violation_index = msg.raw_input("> ")
        if re.match("^\d+$", violation_index) is None or\
           int(violation_index) < 1 or\
           int(violation_index) > len(violations):
          msg.fail("Must provide an integer between 1 and %d!" % len(violations))
          continue
        self.bug_signature = violations[int(violation_index)-1]
    else:
      self.bug_signature = violations[0]
    msg.success("\nBug signature to match is %s. Proceeding with MCS finding!\n" % self.bug_signature)

    self.transform_dag = transform_dag
    # A second log with just our MCS progress log messages
    self._extra_log = extra_log
    self.kwargs = kwargs
    self.end_wait_seconds = end_wait_seconds
    self.wait_on_deterministic_values = wait_on_deterministic_values
    # `no' means "number"
    self.no_violation_verification_runs = no_violation_verification_runs
    self._runtime_stats = RuntimeStats(self.subsequence_id, runtime_stats_path=runtime_stats_path)
    # Whether to try alternate trace splitting techiques besides splitting by time.
    self.optimized_filtering = optimized_filtering
    self.forker = forker
    self.replay_final_trace = replay_final_trace
    self.strict_assertion_checking = strict_assertion_checking
    self.delay_flow_mods = delay_flow_mods

  def log(self, s):
    ''' Output a message to both self._log and self._extra_log '''
    msg.mcs_event(s)
    if self._extra_log is not None:
      self._extra_log.write(s + '\n')
      self._extra_log.flush()

  def log_violation(self, s):
    ''' Output a message to both self._log and self._extra_log '''
    msg.mcs_event(color.RED + s)
    if self._extra_log is not None:
      self._extra_log.write(s + '\n')
      self._extra_log.flush()

  def log_no_violation(self, s):
    ''' Output a message to both self._log and self._extra_log '''
    msg.mcs_event(color.GREEN + s)
    if self._extra_log is not None:
      self._extra_log.write(s + '\n')
      self._extra_log.flush()

  def init_results(self, results_dir):
    ''' Precondition: results_dir exists, and is clean (preferably
    initialized by experiments/setup.py).'''
    if self._extra_log is None:
      self._extra_log = open("%s/mcs_finder.log" % results_dir, "w")
    if self._runtime_stats.get_runtime_stats_path() is None:
      runtime_stats_path = "%s/runtime_stats.json" % results_dir
      self._runtime_stats.set_runtime_stats_path(runtime_stats_path)
    if self.mcs_trace_path is None:
      self.mcs_trace_path = "%s/mcs.trace" % results_dir
    # TODO(cs): assumes that transform dag is a peeker, not some other
    # transformer
    peeker_exists = self.transform_dag is not None
    self.mcs_log_tracker = MCSLogTracker(results_dir, self.mcs_trace_path,
                                         self._runtime_stats,
                                         self.simulation_cfg, peeker_exists)
    self.replay_log_tracker = ReplayLogTracker(results_dir)

  # N.B. only called in the parent process.
  def simulate(self, check_reproducability=True):
    self._runtime_stats.set_dag_stats(self.dag)

    # apply domain knowledge: treat failure/recovery pairs atomically, and
    # filter event types we don't want to include in the MCS
    # (e.g. CheckInvariants)
    self.dag.mark_invalid_input_sequences()
    # TODO(cs): invoke dag.filter_unsupported_input_types()

    if len(self.dag) == 0:
      raise RuntimeError("No supported input types?")

    if check_reproducability:
      # First, run through without pruning to verify that the violation exists
      self._runtime_stats.record_replay_start()

      for i in range(0, self.no_violation_verification_runs):
        bug_found = self.replay(self.dag, "reproducibility",
                                ignore_runtime_stats=True)
        if bug_found:
          break
      self._runtime_stats.set_initial_verification_runs_needed(i)
      self._runtime_stats.record_replay_end()
      if not bug_found:
        msg.fail("Unable to reproduce correctness violation!")
        sys.exit(5)
      self.log("Violation reproduced successfully! Proceeding with pruning")

    self._runtime_stats.record_prune_start()

    # Run optimizations.
    # TODO(cs): Better than a boolean flag: check if
    # log(len(self.dag)) > number of input types to try
    if self.optimized_filtering:
      self._optimize_event_dag()
    precompute_cache = PrecomputeCache()

    # Invoke delta debugging
    (dag, total_inputs_pruned) = self._ddmin(self.dag, 2, precompute_cache=precompute_cache)
    # Make sure to track the final iteration size
    self._track_iteration_size(total_inputs_pruned)
    self.dag = dag

    self.log("=== Total replays: %d ===" % self._runtime_stats.total_replays)
    self._runtime_stats.record_prune_end()
    self.mcs_log_tracker.dump_runtime_stats()
    self.log("Final MCS (%d elements):" % len(self.dag.input_events))
    for i in self.dag.input_events:
      self.log(" - %s" % str(i))

    if self.replay_final_trace:
      #  Replaying the final trace achieves two goals:
      #  - verifies that the MCS indeed ends in the violation
      #  - allows us to prune internal events that time out
      bug_found = self.replay(self.dag, "final_mcs_trace",
                              ignore_runtime_stats=True)
      if not bug_found:
        self.log('''Warning: MCS did not result in violation. Trying replay '''
                 '''again without timed out events.''')
        # TODO(cs): always replay the MCS without timeouts, since the console
        # output will be significantly cleaner?
        no_timeouts = self.dag.filter_timeouts()
        bug_found = self.replay(no_timeouts, "final_mcs_no_timed_out_events",
                                ignore_runtime_stats=True)
        if not bug_found:
          self.log('''Warning! Final MCS did not result in violation, even '''
                   ''' after ignoring timed out internal events. '''
                   ''' See tools/visualization/visualize1D.html for debugging''')

    # N.B. dumping the MCS trace must occur after the final replay trace,
    # since we need to infer which events will time out for events.trace.notimeouts
    if self.mcs_trace_path is not None:
      self.mcs_log_tracker.dump_mcs_trace(self.dag, self)
    return ExitCode(0)

  # N.B. always called within a child process.
  def _ddmin(self, dag, split_ways, precompute_cache=None, label_prefix=(),
             total_inputs_pruned=0):
    # This is the delta-debugging algorithm from:
    #   http://www.st.cs.uni-saarland.de/papers/tse2002/tse2002.pdf,
    # Section 3.2
    # TODO(cs): we could do much better if we leverage domain knowledge (e.g.,
    # start by pruning all LinkFailures, or splitting by nodes rather than
    # time)
    if split_ways > len(dag.input_events):
      self.log("Done")
      return (dag, total_inputs_pruned)

    local_label = lambda i, inv=False: "%s%d/%d" % ("~" if inv else "", i, split_ways)
    subset_label = lambda label: ".".join(map(str, label_prefix + ( label, )))
    print_subset = lambda label, s: subset_label(label) + ": "+" ".join(map(lambda e: e.label, s))

    subsets = split_list(dag.input_events, split_ways)
    self.log("Subsets:\n"+"\n".join(print_subset(local_label(i), s) for i, s in enumerate(subsets)))
    for i, subset in enumerate(subsets):
      label = local_label(i)
      new_dag = dag.input_subset(subset)
      input_sequence = tuple(new_dag.input_events)
      self.log("Current subset: %s" % print_subset(label, input_sequence))
      if precompute_cache.already_done(input_sequence):
        self.log("Already computed. Skipping")
        continue
      precompute_cache.update(input_sequence)
      if input_sequence == ():
        self.log("Subset after pruning dependencies was empty. Skipping")
        continue

      self._track_iteration_size(total_inputs_pruned)
      violation = self._check_violation(new_dag, i, label)
      if violation:
        self.log_violation("Subset %s reproduced violation. Subselecting." % subset_label(label))
        self.mcs_log_tracker.maybe_dump_intermediate_mcs(new_dag,
                                                         subset_label(label), self)

        total_inputs_pruned += len(dag.input_events) - len(new_dag.input_events)
        return self._ddmin(new_dag, 2, precompute_cache=precompute_cache,
                           label_prefix = label_prefix + (label, ),
                           total_inputs_pruned=total_inputs_pruned)

    self.log_no_violation("No subsets with violations. Checking complements")
    for i, subset in enumerate(subsets):
      label = local_label(i, True)
      prefix = label_prefix + (label, )
      new_dag = dag.input_complement(subset)
      input_sequence = tuple(new_dag.input_events)
      self.log("Current complement: %s" % print_subset(label, input_sequence))
      if precompute_cache.already_done(input_sequence):
        self.log("Already computed. Skipping")
        continue
      precompute_cache.update(input_sequence)

      if input_sequence == ():
        self.log("Subset %s after pruning dependencies was empty. Skipping", subset_label(label))
        continue

      self._track_iteration_size(total_inputs_pruned)
      violation = self._check_violation(new_dag, i, label)
      if violation:
        self.log_violation("Subset %s reproduced violation. Subselecting." % subset_label(label))
        self.mcs_log_tracker.maybe_dump_intermediate_mcs(new_dag,
                                                         subset_label(label), self)
        total_inputs_pruned += len(dag.input_events) - len(new_dag.input_events)
        return self._ddmin(new_dag, max(split_ways - 1, 2),
                           precompute_cache=precompute_cache,
                           label_prefix=prefix,
                           total_inputs_pruned=total_inputs_pruned)

    self.log_no_violation("No complements with violations.")
    if split_ways < len(dag.input_events):
      self.log("Increasing granularity.")
      return self._ddmin(dag, min(len(dag.input_events), split_ways*2),
                         precompute_cache=precompute_cache,
                         label_prefix=label_prefix,
                         total_inputs_pruned=total_inputs_pruned)
    return (dag, total_inputs_pruned)

  # N.B. always called within a child process.
  def _track_iteration_size(self, total_inputs_pruned):
    self._runtime_stats.record_iteration_size(len(self.dag.input_events) - total_inputs_pruned)

  # N.B. always called within a child process.
  def _check_violation(self, new_dag, subset_index, label):
    ''' Check if there were violations '''
    # Try no_violation_verification_runs times to see if the bug shows up
    for i in range(0, self.no_violation_verification_runs):
      bug_found = self.replay(new_dag, label)

      if bug_found:
        # Violation in the subset
        self.log_violation("Violation! Considering %d'th" % subset_index)
        self._runtime_stats.record_violation_found(i)
        return True

    # No violation!
    self.log_no_violation("No violation in %d'th..." % subset_index)
    return False

  def replay(self, new_dag, label, ignore_runtime_stats=False):
    # Run the simulation forward
    if self.transform_dag:
      new_dag = self.transform_dag(new_dag)

    self._runtime_stats.record_replay_stats(len(new_dag.input_events))

    # N.B. this function is run as a child process.
    def play_forward(results_dir, subsequence_id):
      # TODO(cs): need to serialize the parameters to Replayer rather than
      # wrapping them in a closure... otherwise, can't use RemoteForker
      # TODO(aw): MCSFinder needs to configure Simulation to always let DataplaneEvents pass through
      create_clean_python_dir(results_dir)

      # Copy stdout and stderr to a file "replay.out"
      tee = Tee(open(os.path.join(results_dir, "replay.out"), "w"))
      tee.tee_stdout()
      tee.tee_stderr()

      # Set up replayer.
      input_logger = InputLogger()
      replayer = Replayer(self.simulation_cfg, new_dag,
                          wait_on_deterministic_values=self.wait_on_deterministic_values,
                          input_logger=input_logger,
                          allow_unexpected_messages=False,
                          pass_through_whitelisted_messages=True,
                          delay_flow_mods=self.delay_flow_mods,
                          **self.kwargs)
      replayer.init_results(results_dir)
      self._runtime_stats = RuntimeStats(subsequence_id)
      violations = []
      simulation = None
      try:
        simulation = replayer.simulate()
        self._track_new_internal_events(simulation, replayer)
        # Wait a bit in case the bug takes awhile to happen
        self.log("Sleeping %d seconds after run" % self.end_wait_seconds)
        time.sleep(self.end_wait_seconds)
        violations = self.invariant_check(simulation)
        if violations != []:
          input_logger.log_input_event(InvariantViolation(violations))
      except SystemExit:
        # One of the invariant checks bailed early. Oddly, this is not an
        # error for us, it just means that there were no violations...
        # [this logic is arguably broken]
        # Return no violations, and let Forker handle system exit for us.
        violations = []
      finally:
        input_logger.close(replayer, self.simulation_cfg, skip_mcs_cfg=True)
        if simulation is not None:
          simulation.clean_up()
        tee.close()
      if self.strict_assertion_checking:
        test_serialize_response(violations, self._runtime_stats.client_dict())
      timed_out_internal = [ e.label for e in new_dag.events if e.timed_out ]
      return (violations, self._runtime_stats.client_dict(), timed_out_internal)

    # TODO(cs): once play_forward() is no longer a closure, register it only once
    self.forker.register_task("play_forward", play_forward)
    results_dir = self.replay_log_tracker.get_replay_logger_dir(label)
    self.subsequence_id += 1
    (violations, client_runtime_stats,
                 timed_out_internal) = self.forker.fork("play_forward",
                                                        results_dir,
                                                        self.subsequence_id)
    new_dag.set_events_as_timed_out(timed_out_internal)

    bug_found = False
    if violations != []:
      msg.fail("Violations: %s" % str(violations))
      if self.bug_signature in violations:
        bug_found = True
      else:
        msg.fail("Bug does not match initial violation fingerprint!")
    else:
      msg.success("No correctness violations!")

    if not ignore_runtime_stats:
      self._runtime_stats.merge_client_dict(client_runtime_stats)

    return bug_found

  def _optimize_event_dag(self):
    ''' Employs domain knowledge of event classes to reduce the size of event
    dag. Currently prunes event types.'''
    # TODO(cs): Another approach for later: split by nodes
    event_types = [TrafficInjection, DataplaneDrop, SwitchFailure,
                   SwitchRecovery, LinkFailure, LinkRecovery, HostMigration,
                   ControllerFailure, ControllerRecovery, PolicyChange, ControlChannelBlock,
                   ControlChannelUnblock]
    for event_type in event_types:
      pruned = [e for e in self.dag.input_events if not isinstance(e, event_type)]
      if len(pruned)==len(self.dag.input_events):
        self.log("\t** No events pruned for event type %s. Next!" % event_type)
        continue
      pruned_dag = self.dag.input_complement(pruned)
      bug_found = self.replay(pruned_dag, "opt_%s" % event_type.__name__)
      if bug_found:
        self.log("\t** VIOLATION for pruning event type %s! Resizing original dag" % event_type)
        self.dag = pruned_dag

  # N.B. always called within a child process.
  def _track_new_internal_events(self, simulation, replayer):
    ''' Pre: simulation must have been run through a replay'''
    # We always check against internal events that were buffered at the end of
    # the original run (don't want to overcount)
    prev_buffered_receives = []
    try:
      path = self.superlog_path + ".unacked"
      if not os.path.exists(path):
        log.warn("unacked internal events file from original run does not exist")
        return
      prev_buffered_receives = set([ e.pending_receive for e in
                                     [ f for f in EventDag(log_parser.parse_path(path)).events
                                       if type(f) == ControlMessageReceive ] ])
    except ValueError as e:
      log.warn("unacked internal events is corrupt? %r" % e)
      return
    buffered_message_receipts = []
    for p in simulation.openflow_buffer.pending_receives():
      if p not in prev_buffered_receives:
        buffered_message_receipts.append(repr(p))
      else:
        prev_buffered_receives.remove(p)

    self._runtime_stats.record_buffered_message_receipts(buffered_message_receipts)
    new_internal_events = replayer.unexpected_state_changes + replayer.passed_unexpected_messages
    self._runtime_stats.record_new_internal_events(new_internal_events)
    self._runtime_stats.record_early_internal_events(replayer.early_state_changes)
    self._runtime_stats.record_timed_out_events(dict(replayer.event_scheduler_stats.event2timeouts))
    self._runtime_stats.record_matched_events(dict(replayer.event_scheduler_stats.event2matched))


# TODO(cs): Hack alert. Shouldn't be a subclass
class EfficientMCSFinder(MCSFinder):
  ''' Exactly the same functionality as MCSFinder, but assumes that
  indeterminate results cannot occur. Worst-case runtime of O(n) as opposed to
  O(n^2) replays. Taken from the predecessor paper:
     http://www.st.cs.uni-saarland.de/publications/files/zeller-esec-1999.pdf
  Section 4
  '''
  # N.B. always called within a child process.
  def _ddmin(self, dag, carryover_inputs, precompute_cache=None,
             recursion_level=0, label_prefix=(), total_inputs_pruned=0):
    ''' carryover_inputs is the variable "r" from the paper. '''
    # Hack: superclass calls _ddmin with an integer, which doesn't match our
    # API. Translate that to an empty sequence. (we also don't use precompute_cache)
    if type(carryover_inputs) == int:
      carryover_inputs = []

    local_label = lambda i: "%s/%d" % ("l" if i == 0 else "r", recursion_level)
    subset_label = lambda label: ".".join(map(str, label_prefix + ( label, )))
    print_subset = lambda label, s: subset_label(label) + ": "+" ".join(map(lambda e: e.label, s))

    # Base case. Note that atomic_inputs are grouped-together failure/recovery
    # pairs, or normal inputs otherwise.
    if len(dag.atomic_input_events) == 1:
      self.log("Base case %s" % str(dag.input_events))
      return (dag, total_inputs_pruned)

    (left, right) = split_list(dag.atomic_input_events, 2)
    self.log("Subsets:\n"+"\n".join(print_subset(local_label(i), s)
                                    for i, s in enumerate([left,right])))
    # This is: [dag.input_subset(left), dag.input_subset(right)]
    left_right_dag = []

    for i, subsequence in enumerate([left, right]):
      label = local_label(i)
      prefix = label_prefix + (label, )
      new_dag = dag.atomic_input_subset(subsequence)
      self.log("Current subset: %s" % print_subset(label,
                                                   new_dag.atomic_input_events))
      left_right_dag.append(new_dag)
      # We test on subsequence U carryover_inputs
      test_dag = new_dag.insert_atomic_inputs(carryover_inputs)
      self._track_iteration_size(total_inputs_pruned)
      violation = self._check_violation(test_dag, i, label)
      if violation:
        self.log("Violation found in %dth half. Recursing" % i)
        total_inputs_pruned += len(dag.input_events) - len(new_dag.input_events)
        self.mcs_log_tracker.maybe_dump_intermediate_mcs(new_dag, "", self)
        return self._ddmin(new_dag, carryover_inputs,
                           recursion_level=recursion_level+1,
                           label_prefix=prefix,
                           total_inputs_pruned=total_inputs_pruned)

    self.log("Interference")
    (left_dag, right_dag) = left_right_dag
    self.log("Recursing on left half")
    prefix = label_prefix + ("il/%d" % recursion_level,)
    (left_result,
     total_inputs_pruned) = self._ddmin(left_dag,
                                        right_dag.insert_atomic_inputs(carryover_inputs).atomic_input_events,
                                        recursion_level=recursion_level+1,
                                        label_prefix=prefix,
                                        total_inputs_pruned=total_inputs_pruned)
    self.log("Recursing on right half")
    prefix = label_prefix + ("ir/%d" % recursion_level,)
    (right_result,
     total_inputs_pruned) = self._ddmin(right_dag,
                                        left_dag.insert_atomic_inputs(carryover_inputs).atomic_input_events,
                                        recursion_level=recursion_level+1,
                                        label_prefix=prefix,
                                        total_inputs_pruned=total_inputs_pruned)

    return (left_result.insert_atomic_inputs(right_result.atomic_input_events),
            total_inputs_pruned)


class ReplayLogTracker(object):
  ''' Logs intermediate and final replay traces chosen by delta debugging'''
  def __init__(self, results_dir):
    self.results_dir = results_dir
    self.count = 0

  def get_replay_logger_dir(self, label):
    dst = os.path.join(self.results_dir, "interreplay_%d_%s" % (self.count, label.replace("/", "_")))
    self.count += 1
    return dst

class MCSLogTracker(object):
  ''' Logs intermedate and final MCS results that are the outcome(s) of delta
  debugging'''
  def __init__(self, results_dir, mcs_trace_path, runtime_stats,
               simulation_cfg, peeker_exists):
    self.results_dir = results_dir
    self.mcs_trace_path = mcs_trace_path
    self.simulation_cfg = simulation_cfg
    self.peeker_exists = peeker_exists
    self.min_size = sys.maxint
    self.count = 0
    self.runtime_stats = runtime_stats
    self.runtime_stats.set_peeker(self.peeker_exists)
    self.runtime_stats.set_config(str(self.simulation_cfg))

  def dump_runtime_stats(self, runtime_stats_path=None):
    # We clone runtime_stats b/c the runtime_stats_path changes in the case
    # of dumping intermediate MCS runtime stats.
    runtime_stats = self.runtime_stats.clone(runtime_stats_path)
    # TODO(cs): assumes that Peeker is the transformer, and that Peeker is run
    # only in the parent process.
    runtime_stats.ambiguous_counts = dict(Peeker.ambiguous_counts)
    runtime_stats.ambiguous_events = dict(Peeker.ambiguous_events)
    runtime_stats.write_runtime_stats()

  def maybe_dump_intermediate_mcs(self, dag, label, control_flow):
    if len(dag.events) < self.min_size:
      # Only dump if MCS decreases in size
      self.min_size = len(dag.events)
      self.count += 1
      dst = os.path.join(self.results_dir, "intermcs_%d_%s" % (self.count, label.replace("/", "_")))
      create_clean_python_dir(dst)
      self.dump_mcs_trace(dag, control_flow, os.path.join(dst, os.path.basename(self.mcs_trace_path)))
      self.dump_runtime_stats(os.path.join(dst,
          os.path.basename(self.runtime_stats.get_runtime_stats_path())))

  def dump_mcs_trace(self, dag, control_flow, mcs_trace_path=None):
    if mcs_trace_path is None:
      mcs_trace_path = self.mcs_trace_path
    for extension in ["", ".notimeouts"]:
      output_path = mcs_trace_path + extension
      input_logger = InputLogger()
      input_logger.open(os.path.dirname(output_path),
                        output_filename="mcs.trace" + extension)
      for e in dag.events:
        if extension == ".notimeouts" and e.timed_out:
          continue
        input_logger.log_input_event(e)
      input_logger.close(control_flow, self.simulation_cfg, skip_mcs_cfg=True)

class RuntimeStats(object):
  ''' Tracks statistics and configuration information of the delta debugging runs '''

  child_fields = ['iteration_size', 'violation_found_in_run', 'new_internal_events',
                  'early_internal_events', 'timed_out_events',
                  'matched_events', 'buffered_message_receipts']
  child_counters = ['violation_found_in_run']

  def __init__(self, subsequence_id, runtime_stats_path=None):
    ''' runtime_stats_path should only be None if the stats of this replay run
    are only intermediate (to be aggregated into overall stats) and not dumped
    to disk. '''
    # subsequence_id will be 0 for the parent process, and non-zero for all
    # child processes.
    self.subsequence_id = subsequence_id
    self._runtime_stats_path = runtime_stats_path
    # -------------------- Stats set by child processes  -------------------- #
    # { delta debugging subseqence # -> count of remaining events }
    self.iteration_size = {}
    # { verification attempt # -> count of times violation was found at this # }
    self.violation_found_in_run = Counter()
    # { replay iteration -> [string representations new internal events] }
    self.new_internal_events = {}
    # { replay iteration -> [string representations messages buffered at end of run] }
    self.buffered_message_receipts = {}
    # { replay iteration -> [string representations internal events that
    #                        violated causality] }
    self.early_internal_events = {}
    # { replay iteration -> { event type -> timeouts } }
    self.timed_out_events = {}
    # { replay iteration -> { event type -> successful matches } }
    self.matched_events = {}
    # -------------------- Stats set by parent process -------------------- #
    self.total_inputs = 0
    self.total_events = 0
    self.original_duration_seconds = 0
    self.replay_start_epoch = 0
    self.replay_end_epoch = 0
    self.replay_duration_seconds = 0
    self.prune_start_epoch = 0
    self.prune_duration_seconds = 0
    self.initial_verification_runs_needed  = 0
    self.peeker = ""
    self.config = ""
    self.total_replays = 0
    self.total_inputs_replayed = 0
    # { % of inferred fingerprints that were ambiguous ->
    #   # of replays where this % occurred }
    self.ambiguous_counts = {}
    # { class of event -> # occurences of ambiguity }
    self.ambiguous_events = {}

  def write_runtime_stats(self):
    # Now write contents to a file
    now = timestamp_string()

    if self._runtime_stats_path is None:
      # TODO(cs): race condition if multiple MCS processes are running
      self._runtime_stats_path = "runtime_stats/" + now + ".json"

    if os.path.exists(self._runtime_stats_path):
      raise RuntimeError("Runtime stats file %s already exists.." %
              self._runtime_stats_path)

    with file(self._runtime_stats_path, "w") as output:
      json_string = json.dumps(self.__dict__, sort_keys=True, indent=2,
                               separators=(',', ': '))
      output.write(json_string)

  def set_runtime_stats_path(self, runtime_stats_path):
    self._runtime_stats_path = runtime_stats_path

  def get_runtime_stats_path(self):
    return self._runtime_stats_path

  # N.B. always invoked within a child process.
  def clone(self, runtime_stats_path):
    clone = copy.deepcopy(self)
    # If runtime_stats_path is None, this is the main MCS run. Otherwise
    # we are dumping intermediate MCS results.
    if runtime_stats_path is not None:
      clone.set_runtime_stats_path(runtime_stats_path)
    return clone

  # -------------------- Stats set by parent process -------------------- #

  def set_dag_stats(self, dag):
    self.total_inputs = len(dag.input_events)
    self.total_events = len(dag)
    self.original_duration_seconds = \
      (dag.events[-1].time.as_float() -
       dag.events[0].time.as_float())

  def record_replay_start(self):
    self.replay_start_epoch = time.time()

  def record_replay_end(self):
    self.replay_end_epoch = time.time()
    self.replay_duration_seconds = self.replay_end_epoch - self.replay_start_epoch

  def record_prune_start(self):
    self.prune_start_epoch = time.time()

  def record_prune_end(self):
    self.prune_end_epoch = time.time()
    self.prune_duration_seconds = self.prune_end_epoch - self.prune_start_epoch

  def set_initial_verification_runs_needed(self, verification_runs):
    self.initial_verification_runs_needed = verification_runs

  def set_peeker(self, peeker):
    self.peeker = peeker

  def set_config(self, config):
    self.config = config

  def record_replay_stats(self, number_inputs_replayed):
    ''' Should be invoked once for every replay '''
    self.total_replays += 1
    self.total_inputs_replayed += number_inputs_replayed

  # -------------------- Stats set by child processes  -------------------- #

  def record_iteration_size(self, iteration_size):
    self.iteration_size[self.subsequence_id] = iteration_size

  def record_violation_found(self, verification_iteration):
    if type(self.violation_found_in_run) != Counter:
      self.violation_found_in_run = Counter(self.violation_found_in_run)
    self.violation_found_in_run[verification_iteration] += 1

  def record_buffered_message_receipts(self, buffered_message_receipts):
    self.buffered_message_receipts[self.subsequence_id] = buffered_message_receipts

  def record_new_internal_events(self, new_internal_events):
    self.new_internal_events[self.subsequence_id] = new_internal_events

  def record_early_internal_events(self, early_internal_events):
    self.early_internal_events[self.subsequence_id] = early_internal_events

  def record_timed_out_events(self, timed_out_events):
    self.timed_out_events[self.subsequence_id] = timed_out_events

  def record_matched_events(self, matched_events):
    self.matched_events[self.subsequence_id] = matched_events

  # -------------------- RPC helper methods -------------------- #

  def client_dict(self):
    ''' Return a serializable dict '''
    # Only include relevent fields for parent
    d = {}
    for field in RuntimeStats.child_fields:
      v = getattr(self, field)
      # xmlrpclib doesn't allow non-string keys
      if type(v) == Counter:
        v = dict(v)
      if type(v) == dict:
        v = dict((str(key), value) for key, value in v.items())
      d[field] = v
    return d

  def merge_client_dict(self, client_dict):
    for field, value in client_dict.iteritems():
      try:
        field = int(field)
      except:
        pass
      if field in RuntimeStats.child_counters:
        for k, count in value.iteritems():
          getattr(self, field)[k] += count
      if type(value) == dict:
        setattr(self, field, dict(getattr(self, field).items() + value.items()))
      elif type(value) == int:
        setattr(self, field, getattr(self, field) + value)
      else:
        raise ValueError("Unknown field %s: %s" % (str(field),str(value)))
