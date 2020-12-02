import os
import time

import numpy as np
import six

from tensorflow.core.framework.summary_pb2 import Summary
from tensorflow.core.protobuf import config_pb2
from tensorflow.core.util.event_pb2 import SessionLog
from tensorflow.python.client import timeline
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import errors
from tensorflow.python.framework import meta_graph
from tensorflow.python.framework import ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.platform import gfile
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.training import session_run_hook
from tensorflow.python.training import training_util
from tensorflow.python.training.session_run_hook import SessionRunArgs
from tensorflow.python.training.summary_io import SummaryWriterCache
from tensorflow.python.util.tf_export import tf_export


# checkpoints only at end of training

class CustomCheckpointSaverHook(session_run_hook.SessionRunHook):
  """Saves checkpoints every N steps or seconds."""

  def __init__(self,
               checkpoint_dir,
               saver=None,
               checkpoint_basename="model.ckpt",
               scaffold=None,
               listeners=None,
               save_graph_def=True):
    """Initializes a `CheckpointSaverHook`.
    Args:
      checkpoint_dir: `str`, base directory for the checkpoint files.
      save_secs: `int`, save every N secs.
      save_steps: `int`, save every N steps.
      saver: `Saver` object, used for saving.
      checkpoint_basename: `str`, base name for the checkpoint files.
      scaffold: `Scaffold`, use to get saver object.
      listeners: List of `CheckpointSaverListener` subclass instances. Used for
        callbacks that run immediately before or after this hook saves the
        checkpoint.
      save_graph_def: Whether to save the GraphDef and MetaGraphDef to
        `checkpoint_dir`. The GraphDef is saved after the session is created as
        `graph.pbtxt`. MetaGraphDefs are saved out for every checkpoint as
        `model.ckpt-*.meta`.
    Raises:
      ValueError: One of `save_steps` or `save_secs` should be set.
      ValueError: At most one of `saver` or `scaffold` should be set.
    """
    logging.info("Create CheckpointSaverHook.")
    if saver is not None and scaffold is not None:
      raise ValueError("You cannot provide both saver and scaffold.")
    self._saver = saver
    self._checkpoint_dir = checkpoint_dir
    self._save_path = os.path.join(checkpoint_dir, checkpoint_basename)
    self._scaffold = scaffold
    self._listeners = listeners or []
    self._steps_per_run = 1
    self._save_graph_def = save_graph_def

  def _set_steps_per_run(self, steps_per_run):
    self._steps_per_run = steps_per_run

  def begin(self):
    self._summary_writer = SummaryWriterCache.get(self._checkpoint_dir)
    self._global_step_tensor = training_util._get_or_create_global_step_read()  # pylint: disable=protected-access
    if self._global_step_tensor is None:
      raise RuntimeError(
          "Global step should be created to use CheckpointSaverHook.")
    for l in self._listeners:
      l.begin()

  def after_create_session(self, session, coord):
    global_step = session.run(self._global_step_tensor)
    if self._save_graph_def:
      # We do write graph and saver_def at the first call of before_run.
      # We cannot do this in begin, since we let other hooks to change graph and
      # add variables in begin. Graph is finalized after all begin calls.
      training_util.write_graph(
          ops.get_default_graph().as_graph_def(add_shapes=True),
          self._checkpoint_dir, "graph.pbtxt")
    saver_def = self._get_saver().saver_def if self._get_saver() else None
    graph = ops.get_default_graph()
    meta_graph_def = meta_graph.create_meta_graph_def(
        graph_def=graph.as_graph_def(add_shapes=True), saver_def=saver_def)
    self._summary_writer.add_graph(graph)
    self._summary_writer.add_meta_graph(meta_graph_def)
    # The checkpoint saved here is the state at step "global_step".
    self._save(session, global_step)

  def before_run(self, run_context):  # pylint: disable=unused-argument
    return SessionRunArgs(self._global_step_tensor)

  def after_run(self, run_context, run_values):
    pass

  def end(self, session):
    last_step = session.run(self._global_step_tensor)
    self._save(session, last_step)
    for l in self._listeners:
      l.end(session, last_step)

  def _save(self, session, step):
    """Saves the latest checkpoint, returns should_stop."""
    logging.info("Calling checkpoint listeners before saving checkpoint %d...",
                 step)
    print('Custom CheckpointSaverHook saving final checkpoint.')
    for l in self._listeners:
      l.before_save(session, step)

    logging.info("Saving checkpoints for %d into %s.", step, self._save_path)
    self._get_saver().save(session, self._save_path, global_step=step,
                           write_meta_graph=self._save_graph_def)
    self._summary_writer.add_session_log(
        SessionLog(
            status=SessionLog.CHECKPOINT, checkpoint_path=self._save_path),
        step)
    logging.info("Calling checkpoint listeners after saving checkpoint %d...",
                 step)
    should_stop = False
    for l in self._listeners:
      if l.after_save(session, step):
        logging.info(
            "A CheckpointSaverListener requested that training be stopped. "
            "listener: {}".format(l))
        should_stop = True
    return should_stop

  def _get_saver(self):
    if self._saver is not None:
      return self._saver
    elif self._scaffold is not None:
      return self._scaffold.saver

    # Get saver from the SAVERS collection if present.
    collection_key = ops.GraphKeys.SAVERS
    savers = ops.get_collection(collection_key)
    if not savers:
      raise RuntimeError(
          "No items in collection {}. Please add a saver to the collection "
          "or provide a saver or scaffold.".format(collection_key))
    elif len(savers) > 1:
      raise RuntimeError(
          "More than one item in collection {}. "
          "Please indicate which one to use by passing it to the constructor."
          .format(collection_key))

    self._saver = savers[0]
    return savers[0]