# Copyright 2022 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for garbage collecting artifacts."""

import collections
import itertools
from typing import Mapping, Optional, Sequence

from absl import logging
from tfx import types
from tfx.dsl.io import fileio
from tfx.orchestration import data_types_utils
from tfx.orchestration import metadata
from tfx.orchestration import node_proto_view
from tfx.orchestration.experimental.core import task as task_lib
from tfx.orchestration.portable.mlmd import event_lib
from tfx.orchestration.portable.mlmd import execution_lib
from tfx.orchestration.portable.mlmd import store_ext
from tfx.proto.orchestration import garbage_collection_policy_pb2

from tfx.orchestration.experimental.core import garbage_collection_extensions
from ml_metadata.proto import metadata_store_pb2


_KeepOrder = (garbage_collection_policy_pb2.GarbageCollectionPolicy.
              KeepPropertyValueGroups.Grouping.KeepOrder)


def _get_live_output_artifacts_for_node(
    mlmd_handle: metadata.Metadata, node_uid: task_lib.NodeUid
) -> Mapping[str, Sequence[metadata_store_pb2.Artifact]]:
  """Gets all the live output artifacts keyed by output key for `node_uid`."""
  live_output_artifacts_of_node_by_output_key = (
      store_ext.get_live_output_artifacts_of_node_by_output_key(
          mlmd_handle.store,
          pipeline_id=node_uid.pipeline_uid.pipeline_id,
          node_id=node_uid.node_id,
      )
  )
  return {
      output_key: list(itertools.chain.from_iterable(nested_artifact_list))
      for output_key, nested_artifact_list in live_output_artifacts_of_node_by_output_key.items()
  }


def _get_garbage_collection_policies_for_node(
    node: node_proto_view.NodeProtoView
) -> Mapping[str, garbage_collection_policy_pb2.GarbageCollectionPolicy]:
  return {
      output_key: output_spec.garbage_collection_policy
      for output_key, output_spec in node.outputs.outputs.items()
      if output_spec.HasField('garbage_collection_policy')
  }


def _artifacts_not_in_use(
    mlmd_handle: metadata.Metadata,
    artifacts: Sequence[metadata_store_pb2.Artifact],
    events: Sequence[metadata_store_pb2.Event],
) -> Sequence[metadata_store_pb2.Artifact]:
  """Returns artifacts that are not currently in use."""
  artifact_ids = set(a.id for a in artifacts)
  input_events = [
      e for e in events
      if e.artifact_id in artifact_ids and event_lib.is_valid_input_event(e)
  ]
  execution_ids = [e.execution_id for e in input_events]
  if not execution_ids:
    return artifacts
  executions = mlmd_handle.store.get_executions_by_id(execution_ids)
  execution_id_to_execution = {e.id: e for e in executions}
  in_use_artifact_ids = set()
  for event in input_events:
    if event.execution_id not in execution_id_to_execution:
      raise RuntimeError('Could not find execution with id: %d' %
                         event.execution_id)
    execution = execution_id_to_execution[event.execution_id]
    if execution_lib.is_execution_active(execution):
      in_use_artifact_ids.add(event.artifact_id)
  return [a for a in artifacts if a.id not in in_use_artifact_ids]


def _artifacts_to_garbage_collect_for_policy(
    artifacts: Sequence[metadata_store_pb2.Artifact],
    policy: garbage_collection_policy_pb2.GarbageCollectionPolicy,
) -> Sequence[metadata_store_pb2.Artifact]:
  """Returns artifacts that are not kept by the policy."""
  if policy.HasField('keep_most_recently_published'):
    return _artifacts_not_most_recently_published(
        artifacts, policy.keep_most_recently_published)
  elif policy.HasField('keep_property_value_groups'):
    return _artifacts_not_kept_by_property_value_groups(
        artifacts, policy.keep_property_value_groups)
  else:
    logging.error('Skipped garbage collection due to unknown policy: %s',
                  policy)
    return []


def _artifacts_not_most_recently_published(
    artifacts: Sequence[metadata_store_pb2.Artifact],
    keep_most_recently_published: garbage_collection_policy_pb2.GarbageCollectionPolicy.KeepMostRecentlyPublished,
) -> Sequence[metadata_store_pb2.Artifact]:
  """Returns artifacts that are not kept by KeepMostRecentlyPublished."""
  num_artifacts = keep_most_recently_published.num_artifacts
  if num_artifacts <= 0:
    return artifacts
  elif len(artifacts) <= num_artifacts:
    return []
  else:
    # Handle ties if multiple artifacts have the same create_time_since_epoch
    publish_times = sorted([a.create_time_since_epoch for a in artifacts])
    cutoff_publish_time = publish_times[-num_artifacts]
    return [
        a for a in artifacts if a.create_time_since_epoch < cutoff_publish_time
    ]


def _get_property_value(artifact: metadata_store_pb2.Artifact,
                        property_name: str) -> Optional[types.Property]:
  if property_name in artifact.properties:
    return data_types_utils.get_metadata_value(
        artifact.properties[property_name])
  elif property_name in artifact.custom_properties:
    return data_types_utils.get_metadata_value(
        artifact.custom_properties[property_name])
  return None


def _artifacts_not_kept_by_property_value_groups(
    artifacts: Sequence[metadata_store_pb2.Artifact],
    keep_property_value_groups: garbage_collection_policy_pb2.GarbageCollectionPolicy.KeepPropertyValueGroups,
) -> Sequence[metadata_store_pb2.Artifact]:
  """Returns artifacts that are not kept by KeepPropertyValueGroups."""
  artifact_groups = [artifacts]
  for grouping in keep_property_value_groups.groupings:
    next_artifact_groups = []
    all_property_value_type = type(None)
    for artifact_group in artifact_groups:
      artifacts_by_property_value = collections.defaultdict(list)
      for artifact in artifact_group:
        property_value = _get_property_value(artifact, grouping.property_name)
        if property_value is not None:
          if all_property_value_type != type(
              None) and all_property_value_type != type(property_value):
            raise ValueError(
                'Properties from the same group should have a homogenous type '
                f'except NoneType. Expected {all_property_value_type}, but '
                f'passed {type(property_value)}')
          all_property_value_type = type(property_value)
        artifacts_by_property_value[property_value].append(artifact)

      if grouping.keep_num <= 0:
        next_artifact_groups.extend(artifacts_by_property_value.values())
      else:
        sorted_property_values = sorted(
            x for x in artifacts_by_property_value.keys() if x is not None)
        if (grouping.keep_order == _KeepOrder.KEEP_ORDER_UNSPECIFIED or
            grouping.keep_order == _KeepOrder.KEEP_ORDER_LARGEST):
          property_values_to_keep = sorted_property_values[-grouping.keep_num:]
        elif grouping.keep_order == _KeepOrder.KEEP_ORDER_SMALLEST:
          property_values_to_keep = sorted_property_values[:grouping.keep_num]
        else:
          message = f'Unknown keep_order in grouping: {grouping}'
          logging.error(message)
          raise ValueError(message)
        for property_value_to_keep in property_values_to_keep:
          next_artifact_groups.append(
              artifacts_by_property_value[property_value_to_keep])
        if None in artifacts_by_property_value and len(
            property_values_to_keep) < grouping.keep_num:
          # TODO(b/251069580): Currently, it gives the lowest priority to retain
          # for the None-property-value group. Should compare with the default
          # value policy.
          next_artifact_groups.append(artifacts_by_property_value[None])
    artifact_groups = next_artifact_groups
  artifacts_ids_to_keep = []
  for artifact_group in artifact_groups:
    for artifact in artifact_group:
      artifacts_ids_to_keep.append(artifact.id)
  return [a for a in artifacts if a.id not in artifacts_ids_to_keep]


def _artifacts_to_garbage_collect(
    mlmd_handle: metadata.Metadata,
    artifacts: Sequence[metadata_store_pb2.Artifact],
    events: Sequence[metadata_store_pb2.Event],
    policy: garbage_collection_policy_pb2.GarbageCollectionPolicy,
) -> Sequence[metadata_store_pb2.Artifact]:
  """Returns artifacts that should be garbage collected."""
  result = artifacts
  result = _artifacts_to_garbage_collect_for_policy(result, policy)
  result = (
      garbage_collection_extensions.artifacts_not_in_use_in_pipeline_groups(
          mlmd_handle, policy.keep_if_used_in_pipeline_groups, result
      )
  )
  result = _artifacts_not_in_use(mlmd_handle, result, events)
  return result


def _is_artifact_external(artifact: metadata_store_pb2.Artifact) -> bool:
  """Returns True if an artifact is external to the pipeline."""
  return _get_property_value(artifact, 'is_external') == 1


def _delete_artifact_uri(artifact: metadata_store_pb2.Artifact) -> bool:
  """Deletes the artifact's URI and returns True if it can be marked as DELETED.

  Args:
    artifact: The artifact containing the URI to delete.

  Returns:
    True: If the URI is deleted or does not exist. In this case we can safely
      mark the artifact as DELETED in MLMD.
    False: If deleting the artifact URI fails.
  """
  logging.info('Deleting URI %s', artifact.uri)

  try:
    if fileio.isdir(artifact.uri):
      fileio.rmtree(artifact.uri)
    else:
      fileio.remove(artifact.uri)
    return True

  # TODO(kmonte): See if there's some fileio exception list we can catch.
  except Exception:  # pylint: disable=broad-exception-caught
    # If an exception is raised during deletion, there are several cases:
    #
    # Case 1: The artifact URI does not exist (if it has been TTL'd off disk,
    # etc.), and in this case the artifact should still be marked as DELETED.
    #
    # Case 2: The artifact URI exists but removing it still fails (if permission
    # is denied, etc.), and in this case the artifact should not be marked as
    # DELETED.
    #
    # Note that even in Case 2, `fileio` may still raise a FileNotFoundError. So
    # instead of catching FileNotFoundError, we check if the URI does not exit.
    if not fileio.exists(artifact.uri):
      logging.exception(
          'URI %s not found for artifact %s', artifact.uri, artifact
      )
      return True

    logging.exception('Failed to delete artifact %s', artifact)
    return False


def get_artifacts_to_garbage_collect_for_node(
    mlmd_handle: metadata.Metadata,
    node_uid: task_lib.NodeUid,
    node: node_proto_view.NodeProtoView,
) -> Sequence[metadata_store_pb2.Artifact]:
  """Returns output artifacts of the given node to garbage collect."""
  policies_by_output_key = _get_garbage_collection_policies_for_node(node)
  logging.info(
      'Garbage collection policies for node %s: %s',
      node.node_info.id,
      policies_by_output_key,
  )
  if not policies_by_output_key:
    return []

  artifacts_by_output_key = _get_live_output_artifacts_for_node(
      mlmd_handle, node_uid
  )
  if not artifacts_by_output_key:
    return []
  logging.info(
      'Candidate artifacts to garbage collect for node %s : %s',
      node.node_info.id,
      artifacts_by_output_key,
  )

  dedupped_artifact_ids = set()
  for artifact in itertools.chain.from_iterable(
      artifacts_by_output_key.values()
  ):
    dedupped_artifact_ids.add(artifact.id)

  events = mlmd_handle.store.get_events_by_artifact_ids(dedupped_artifact_ids)

  result = []
  for output_key, policy in policies_by_output_key.items():
    if output_key not in artifacts_by_output_key:
      continue
    artifacts_to_garbage_collect_for_output_key = _artifacts_to_garbage_collect(
        mlmd_handle, artifacts_by_output_key[output_key], events, policy)
    result.extend(artifacts_to_garbage_collect_for_output_key)
    logging.info(
        'Artifacts to garbage collect for output key %s: %s',
        output_key,
        artifacts_to_garbage_collect_for_output_key,
    )
  return result


def garbage_collect_artifacts(
    mlmd_handle: metadata.Metadata,
    artifacts: Sequence[metadata_store_pb2.Artifact],
) -> None:
  """Garbage collect the artifacts by deleting the payloads.

  GC first filters out external artifacts, and all remaining internal artifacts
  will have distinct URIs. Therefore, it is valid to erase the file contents
  immediately, rather than setting the intermediate state (MARKED_FOR_DELETION)
  and waiting until all artifacts sharing the same URI are marked for deletion.

  Args:
    mlmd_handle: A handle to the MLMD db.
    artifacts: Artifacts that we want to erase their file contents for GC.
  """
  if not artifacts:
    return
  for artifact in artifacts:
    if _is_artifact_external(artifact):
      # To garbage collect external artifacts, only mark the artifacts as
      # DELETED in MLMD.
      logging.info('Mark external artifact %s as DELETED in MLMD', artifact)
      artifact.state = metadata_store_pb2.Artifact.State.DELETED
    else:
      # To garbage collect internal artifacts, delete the URIs and mark the
      # artifacts as DELETED in MLMD if deleting the URIs is successful.
      if _delete_artifact_uri(artifact):
        logging.info('Mark internal artifact %s as DELETED in MLMD', artifact)
        artifact.state = metadata_store_pb2.Artifact.State.DELETED

  mlmd_handle.store.put_artifacts(artifacts)


def run_garbage_collection_for_node(
    mlmd_handle: metadata.Metadata,
    node_uid: task_lib.NodeUid,
    node: node_proto_view.NodeProtoView) -> None:
  """Garbage collects output artifacts of the given node."""
  logging.info('Garbage collection requested for node %s', node_uid)
  if node.node_info.id != node_uid.node_id:
    raise ValueError(
        f'Node uids do not match for garbage collection: {node.node_info.id} '
        f'and {node_uid.node_id}')
  try:
    # We never want to throw exception while GCing artifacts, since the failure
    # of GC implies issues with the past executions, and failure will cause the
    # current execution to fail, which is undesireable.
    artifacts = get_artifacts_to_garbage_collect_for_node(
        mlmd_handle, node_uid, node
    )
    logging.info(
        'Artifacts to garbage collect for node %s: %s',
        node.node_info.id,
        artifacts,
    )
    garbage_collect_artifacts(mlmd_handle, artifacts)
  except Exception:  # pylint: disable=broad-exception-caught
    logging.exception('Garbage collection for node %s failed', node_uid)
