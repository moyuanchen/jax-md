# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Neural Network Primitives."""

import numpy as onp

import jax
from jax import vmap, jit
import jax.numpy as np

from jax_md import space, dataclasses

import haiku as hk

from collections import namedtuple
from functools import partial
from jax.tree_util import tree_multimap, tree_map

from typing import Callable

# Features used in fixed feature methods

def _behler_parrinello_cutoff_fn(dr, cutoff_distance=8.0):
  """Function of pairwise distance that smoothly goes to zero at the cutoff."""
  # Also returns zero if the pairwise distance is zero,
  # to prevent a particle from interacting with itself.
  return np.where((dr < cutoff_distance) & (dr > 1e-7),
                  0.5 * (np.cos(np.pi * dr / cutoff_distance) + 1), 0)


def radial_symmetry_functions(displacement_or_metric,
                              species,
                              eta,
                              cutoff_distance):
  """Returns a function that computes radial symmetry functions.


  Args:
    displacement: A function that produces an `[N_atoms, M_atoms,
    spatial_dimension]` of particle displacements from particle positions
      specified as an `[N_atoms, spatial_dimension] and `[M_atoms,
      spatial_dimension]` respectively.
    species: An `[N_atoms]` that contains the species of each particle.
    eta: Parameter of radial symmetry function that controls the spatial
      extension.
    cutoff_distance: Neighbors whose distance is larger than cutoff_distance do
      not contribute to each others symmetry functions. The contribution of a
      neighbor to the symmetry function and its derivative goes to zero at this
      distance.

  Returns:
    A function that computes the radial symmetry function from input `[N_atoms,
    spatial_dimension]` and returns `[N_atoms, N_types]` where N_types is the
    number of types of particles in the system.
  """
  metric = space.canonicalize_displacement_or_metric(displacement_or_metric)

  def compute_fun(R):
    _metric = partial(metric)
    _metric = space.map_product(_metric)

    def return_radial(atom_type):
      """Returns the radial symmetry functions for neighbor type atom_type."""
      R_neigh = R[species == atom_type, :]
      dr = _metric(R, R_neigh)
      radial = (np.exp(-eta * dr ** 2) * 
                _behler_parrinello_cutoff_fn(dr, cutoff_distance))
      return np.sum(radial, axis=0)

    return np.stack([return_radial(atom_type) for
                     atom_type in np.unique(species)], axis=1)

  return compute_fun

# Graph neural network primitives

"""
  Our implementation here is based off the outstanding GraphNets library by
  DeepMind at, www.github.com/deepmind/graph_nets. This implementation was also
  heavily influenced by work done by Thomas Keck. We implement a subset of the
  functionality from the graph nets library to be compatable with jax-md
  states and neighbor lists, end-to-end jit compilation, and easy batching.

  Graphs are described by node states, edge states, a global state, and
  outgoing / incoming edges.  

  We provide two components:

    1) A GraphIndependent layer that applies a neural network separately to the
       node states, the edge states, and the globals. This is often used as an
       encoding or decoding step.
    2) A GraphNetwork layer that transforms the nodes, edges, and globals using
       neural networks following Battaglia et al. (). Here, we use
       sum-message-aggregation. 

  The graphs network components implemented here implement identical functions
  to the DeepMind library. However, to be compatible with jax-md, there are
  significant differences in the graph layout used here to the reference
  implementation. See `GraphTuple` for details.
"""

@dataclasses.dataclass
class GraphTuple(object):
    """A struct containing graph data.

    Attributes:
      nodes: For a graph with N_nodes, this is an `[N_nodes, node_dimension]`
        array containing the state of each node in the graph.
      edges: For a graph whose degree is bounded by max_degree, this is an
        `[N_nodes, max_degree, edge_dimension]`. Here `edges[i, j]` is the
        state of the outgoing edge from node `i` to node `edge_idx[i, j]`.
      globals: An array of shape `[global_dimension]`.
      edge_idx: An integer array of shape `[N_nodes, max_degree]` where
        `edge_idx[i, j]` is the id of the jth outgoing edge from node `i`.
        Empty entries (that don't contain an edge) are denoted by
        `edge_idx[i, j] == N_nodes`.
    """
    nodes: np.ndarray
    edges: np.ndarray
    globals: np.ndarray
    edge_idx: np.ndarray


def concatenate_graph_features(graphs: GraphTuple) -> GraphTuple:
  """Given a list of GraphTuple returns a new concatenated GraphTuple.

  Note that currently we do not check that the graphs have consistent edge
  connectivity.
  """
  return GraphTuple(
      nodes=np.concatenate([g.nodes for g in graphs], axis=-1),
      edges=np.concatenate([g.edges for g in graphs], axis=-1),
      globals=np.concatenate([g.globals for g in graphs], axis=-1),
      edge_idx=graphs[0].edge_idx,  # TODO: Check for consistency.
  )


def GraphIndependent(edge_fn: Callable,
                     node_fn: Callable,
                     global_fn: Callable) -> Callable:
  """Applies functions independently to the nodes, edges, and global states.
  """
  identity = lambda x: x
  _node_fn = vmap(node_fn) if node_fn is not None else identity
  _edge_fn = vmap(vmap(edge_fn)) if edge_fn is not None else identity
  _global_fn = global_fn if global_fn is not None else identity

  def embed_fn(graph):
    return dataclasses.replace(
        graph,
        nodes=_node_fn(graph.nodes),
        edges=_edge_fn(graph.edges),
        globals=_global_fn(graph.globals)
    )
  return embed_fn


def _apply_node_fn(graph, node_fn):
  mask = graph.edge_idx < graph.nodes.shape[0]
  mask = mask[:, :, np.newaxis]

  if graph.edges is not None:
    # TODO: Should we also have outgoing edges?
    flat_edges = np.reshape(graph.edges, (-1, graph.edges.shape[-1]))
    edge_idx = np.reshape(graph.edge_idx, (-1,))
    incoming_edges = jax.ops.segment_sum(
        flat_edges, edge_idx, graph.nodes.shape[0] + 1)[:-1]
    outgoing_edges = np.sum(graph.edges * mask, axis=1)
  else:
    incoming_edges = None
    outgoing_edges = None

  if graph.globals is not None:
    _globals = np.broadcast_to(graph.globals[np.newaxis, :],
                               graph.nodes.shape[:1] + graph.globals.shape)
  else:
    _globals = None

  return node_fn(graph.nodes, incoming_edges, outgoing_edges, _globals)


def _apply_edge_fn(graph, edge_fn):
  if graph.nodes is not None:
    incoming_nodes = graph.nodes[graph.edge_idx]
    outgoing_nodes = np.broadcast_to(
        graph.nodes[:, np.newaxis, :],
        graph.edge_idx.shape + graph.nodes.shape[-1:])
  else:
    incoming_nodes = None
    outgoing_nodes = None

  if graph.globals is not None:
    _globals = np.broadcast_to(graph.globals[np.newaxis, np.newaxis, :],
                               graph.edge_idx.shape + graph.globals.shape)
  else:
    _globals = None

  mask = graph.edge_idx < graph.nodes.shape[0]
  mask = mask[:, :, np.newaxis]
  return edge_fn(graph.edges, incoming_nodes, outgoing_nodes, _globals) * mask


def _apply_global_fn(graph, global_fn):
  nodes = None if graph.nodes is None else np.sum(graph.nodes, axis=0)

  if graph.edges is not None:
    mask = graph.edge_idx < graph.nodes.shape[0]
    mask = mask[:, :, np.newaxis]
    edges = np.sum(graph.edges * mask, axis=(0, 1))
  else:
    edges = None

  return global_fn(nodes, edges, graph.globals)


class GraphNetwork:
  """Implementation of a Graph Network.

  See https://arxiv.org/abs/1806.01261 for more details.
  """
  def __init__(self, edge_fn, node_fn, global_fn):
    self._node_fn = (None if node_fn is None else
                     partial(_apply_node_fn, node_fn=vmap(node_fn)))

    self._edge_fn = (None if edge_fn is None else
                     partial(_apply_edge_fn, edge_fn=vmap(vmap(edge_fn))))

    self._global_fn = (None if global_fn is None else
                       partial(_apply_global_fn, global_fn=global_fn))

  def __call__(self, graph):
    if self._edge_fn is not None:
      graph = dataclasses.replace(graph, edges=self._edge_fn(graph))

    if self._node_fn is not None:
      graph = dataclasses.replace(graph, nodes=self._node_fn(graph))

    if self._global_fn is not None:
      graph = dataclasses.replace(graph, globals=self._global_fn(graph))

    return graph


# Prefab Networks


class GraphNetEncoder(hk.Module):
  """Implements a Graph Neural Network for energy fitting.

  Based on the network used in "Unveiling the predictive power of static
  structure in glassy systems"; Bapst et al.
  (https://www.nature.com/articles/s41567-020-0842-8). This network first
  embeds edges, nodes, and global state. Then `n_recurrences` of GraphNetwork
  layers are applied. Unlike in Bapst et al. this network does not include a
  readout, which should be added separately depending on the application.

  For example, when predicting particle mobilities, one would use a decoder
  only on the node states while a model of energies would decode only the node
  states.
  """
  def __init__(self, n_recurrences, mlp_sizes, mlp_kwargs=None,
               name='GraphNetEncoder'):
    super(GraphNetEncoder, self).__init__(name=name)

    if mlp_kwargs is None:
      mlp_kwargs = {}

    self._n_recurrences = n_recurrences

    embedding_fn = lambda name: hk.nets.MLP(
        output_sizes=mlp_sizes,
        activate_final=True,
        name=name,
        **mlp_kwargs)

    model_fn = lambda name: lambda *args: hk.nets.MLP(
        output_sizes=mlp_sizes,
        activate_final=True,
        name=name,
        **mlp_kwargs)(np.concatenate(args, axis=-1))

    self._encoder = GraphIndependent(
        embedding_fn('EdgeEncoder'),
        embedding_fn('NodeEncoder'),
        embedding_fn('GlobalEncoder'))
    self._propagation_network = lambda: GraphNetwork(
        model_fn('EdgeFunction'),
        model_fn('NodeFunction'),
        model_fn('GlobalFunction'))

  def __call__(self, graph: GraphTuple) -> GraphTuple:
    encoded = self._encoder(graph)
    outputs = encoded

    for _ in range(self._n_recurrences):
      inputs = concatenate_graph_features([outputs, encoded])
      outputs = self._propagation_network()(inputs)

    return outputs