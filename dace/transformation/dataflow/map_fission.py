# Copyright 2019-2021 ETH Zurich and the DaCe authors. All rights reserved.
""" Map Fission transformation. """

import warnings

from copy import deepcopy as dcpy
from collections import defaultdict
from dace import registry, sdfg as sd, memlet as mm, subsets, data as dt, symbolic
from dace.codegen import control_flow as cf
from dace.sdfg import nodes, graph as gr
from dace.sdfg import utils as sdutil
from dace.sdfg.graph import OrderedDiGraph
from dace.sdfg.propagation import propagate_memlets_state, propagate_subset
from dace.symbolic import pystr_to_symbolic
from dace.transformation import transformation, helpers
from typing import Any, Dict, List, Optional, Tuple


class MapFission(transformation.SingleStateTransformation):
    """ Implements the MapFission transformation.
        Map fission refers to subsuming a map scope into its internal subgraph,
        essentially replicating the map into maps in all of its internal
        components. This also extends the dimensions of "border" transient
        arrays (i.e., those between the maps), in order to retain program
        semantics after fission.

        There are two cases that match map fission:
        
            1. A map with an arbitrary subgraph with more than one computational
               (i.e., non-access) node. The use of arrays connecting the
               computational nodes must be limited to the subgraph, and non
               transient arrays may not be used as "border" arrays.
            2. A map with one internal node that is a nested SDFG, in which
               each state matches the conditions of case (1).

        If a map has nested SDFGs in its subgraph, they are not considered in
        the case (1) above, and MapFission must be invoked again on the maps
        with the nested SDFGs in question.
    """
    map_entry = transformation.PatternNode(nodes.EntryNode)
    nested_sdfg = transformation.PatternNode(nodes.NestedSDFG)

    @staticmethod
    def annotates_memlets():
        return False

    @classmethod
    def expressions(cls):
        return [
            sdutil.node_path_graph(cls.map_entry),
            sdutil.node_path_graph(cls.map_entry, cls.nested_sdfg),
        ]

    @staticmethod
    def _components(subgraph: gr.SubgraphView) -> List[Tuple[nodes.Node, nodes.Node]]:
        """
        Returns the list of tuples non-array components in this subgraph.
        Each element in the list is a 2 tuple of (input node, output node) of
        the component.
        """
        graph = (subgraph if isinstance(subgraph, sd.SDFGState) else subgraph.graph)
        schildren = subgraph.scope_children()
        ns = [(n, graph.exit_node(n)) if isinstance(n, nodes.EntryNode) else (n, n) for n in schildren[None]
              if isinstance(n, (nodes.CodeNode, nodes.EntryNode))]

        return ns

    @staticmethod
    def _border_arrays(sdfg, parent, subgraph):
        """ Returns a set of array names that are local to the fission
            subgraph. """
        nested = isinstance(parent, sd.SDFGState)
        schildren = subgraph.scope_children()
        subset = gr.SubgraphView(parent, schildren[None])
        if nested:
            return set(node.data for node in subset.nodes()
                       if isinstance(node, nodes.AccessNode) and sdfg.arrays[node.data].transient)
        else:
            return set(node.data for node in subset.nodes() if isinstance(node, nodes.AccessNode))

    @staticmethod
    def _internal_border_arrays(total_components, subgraphs):
        """ Returns the set of border arrays that appear between computational
            components (i.e., without sources and sinks). """
        inputs = set()
        outputs = set()

        for components, subgraph in zip(total_components, subgraphs):
            for component_in, component_out in components:
                for e in subgraph.in_edges(component_in):
                    if isinstance(e.src, nodes.AccessNode):
                        inputs.add(e.src.data)
                for e in subgraph.out_edges(component_out):
                    if isinstance(e.dst, nodes.AccessNode):
                        outputs.add(e.dst.data)

        return inputs & outputs

    @staticmethod
    def _outside_map(node, scope_dict, entry_nodes):
        """ Returns True iff node is not in any of the scopes spanned by
            entry_nodes. """
        while scope_dict[node] is not None:
            if scope_dict[node] in entry_nodes:
                return False
            node = scope_dict[node]
        return True

    def can_be_applied(self, graph, expr_index, sdfg, permissive=False):
        map_node = self.map_entry
        nsdfg_node = None

        # If the map is dynamic-ranged, the resulting border arrays would be
        # dynamically sized
        if sd.has_dynamic_map_inputs(graph, map_node):
            return False

        if expr_index == 0:  # Map with subgraph
            subgraphs = [graph.scope_subgraph(map_node, include_entry=False, include_exit=False)]
        else:  # Map with nested SDFG
            nsdfg_node = dcpy(self.nested_sdfg)
            # Make sure there are no other internal nodes in the map
            if len(set(e.dst for e in graph.out_edges(map_node))) > 1:
                return False

            # Get NestedSDFG control flow components
            cf_comp = helpers.find_sdfg_control_flow(nsdfg_node.sdfg)
            if len(cf_comp) == 1:
                cf_item = list(cf_comp.values())[0][1]
                if not isinstance(cf_item, cf.SingleState):
                    return False
            else:
                helpers.nest_sdfg_control_flow(nsdfg_node.sdfg, cf_comp)

            subgraphs = list(nsdfg_node.sdfg.nodes())

        # Test subgraphs
        border_arrays = set()
        total_components = []
        for sg in subgraphs:
            components = self._components(sg)
            snodes = sg.nodes()
            # Test that the subgraphs have more than one computational component
            if expr_index == 0 and len(snodes) > 0 and len(components) <= 1:
                return False

            # Test that the components are connected by transients that are not
            # used anywhere else
            border_arrays |= self._border_arrays(nsdfg_node.sdfg if expr_index == 1 else sdfg,
                                                 sg if expr_index == 1 else graph, sg)
            total_components.append(components)

            # In nested SDFGs and subgraphs, ensure none of the border
            # values are non-transients
            for array in border_arrays:
                if expr_index == 0:
                    ndesc = sdfg.arrays[array]
                else:
                    ndesc = nsdfg_node.sdfg.arrays[array]

                if ndesc.transient is False:
                    return False

            # In subgraphs, make sure transients are not used/allocated
            # in other scopes or states
            if expr_index == 0:
                # Find all nodes not in subgraph
                not_subgraph = set(n.data for n in graph.nodes() if n not in snodes and isinstance(n, nodes.AccessNode) and sdfg.arrays[n.data].transient)
                not_subgraph.update(
                    set(n.data for s in sdfg.nodes() if s != graph for n in s.nodes()
                        if isinstance(n, nodes.AccessNode) and sdfg.arrays[n.data].transient))

                for _, component_out in components:
                    for e in sg.out_edges(component_out):
                        if isinstance(e.dst, nodes.AccessNode):
                            if e.dst.data in not_subgraph:
                                return False

        return True

    def apply(self, graph: sd.SDFGState, sdfg: sd.SDFG):
        map_entry = self.map_entry
        map_exit = graph.exit_node(map_entry)
        nsdfg_node: Optional[nodes.NestedSDFG] = None

        # Obtain subgraph to perform fission to
        if self.expr_index == 0:  # Map with subgraph
            subgraphs = [(graph, graph.scope_subgraph(map_entry, include_entry=False, include_exit=False))]
            parent = sdfg
        else:  # Map with nested SDFG
            nsdfg_node = self.nested_sdfg
            helpers.nest_sdfg_control_flow(nsdfg_node.sdfg)
            subgraphs = [(state, state) for state in nsdfg_node.sdfg.nodes()]
            parent = nsdfg_node.sdfg
            parent_sdfg = parent.parent_sdfg
        modified_arrays = set()

        # Get map information
        outer_map: nodes.Map = map_entry.map
        mapsize = outer_map.range.size()

        # Add new symbols from outer map to nested SDFG
        # Add new symbols also from the adjacent edge subsets and the data descriptors they carry.
        if self.expr_index == 1:
            map_syms = outer_map.range.free_symbols
            for edge in graph.out_edges(map_entry):
                if edge.data.data:
                    map_syms.update(edge.data.subset.free_symbols)
                if edge.data.data in parent_sdfg.arrays:
                    map_syms.update(parent_sdfg.arrays[edge.data.data].free_symbols)
            for edge in graph.in_edges(map_exit):
                if edge.data.data:
                    map_syms.update(edge.data.subset.free_symbols)
                if edge.data.data in parent_sdfg.arrays:
                    map_syms.update(parent_sdfg.arrays[edge.data.data].free_symbols)
            for sym in map_syms:
                symname = str(sym)
                if symname in outer_map.params:
                    continue
                if symname not in nsdfg_node.symbol_mapping.keys():
                    nsdfg_node.symbol_mapping[symname] = sym
                    nsdfg_node.sdfg.symbols[symname] = graph.symbols_defined_at(nsdfg_node)[symname]

            # Remove map symbols from nested mapping
            for name in outer_map.params:
                if str(name) in nsdfg_node.symbol_mapping:
                    del nsdfg_node.symbol_mapping[str(name)]
                if str(name) in nsdfg_node.sdfg.symbols:
                    del nsdfg_node.sdfg.symbols[str(name)]
            
            # TODO: This was an attempt to fix an issue with MapFission and symbols in the NestedSDFG's symbol mapping
            # depending on other symbols and the Map's parameters. Disabled for now until the issue is clarified.
            # # Clean-up symbols depending on the map parameters
            # to_remove = dict()
            # to_add = dict()
            # for symname, symexpr in nsdfg_node.symbol_mapping.items():
            #     try:
            #         fsymbols = symbolic.pystr_to_symbolic(symexpr).free_symbols
            #     except AttributeError:
            #         fsymbols = set()
            #     if any(str(s) in outer_map.params for s in fsymbols):
            #         to_remove[symname] = symexpr
            #         for s in fsymbols:
            #             if str(s) not in outer_map.params and str(s) not in nsdfg_node.sdfg.symbols:
            #                 to_add[str(s)] = sdfg.symbols[(str(s))]
            # if to_remove:
            #     for symname in to_remove:
            #         print(f"Removing symbol {symname} from nested SDFG {nsdfg_node.label}")
            #         del nsdfg_node.symbol_mapping[symname]
            #         if symname in nsdfg_node.sdfg.symbols:
            #             del nsdfg_node.sdfg.symbols[symname]
            #     nsdfg_node.sdfg.symbols.update(to_add)
            #     init_state = nsdfg_node.sdfg.start_state
            #     pre_init_state = nsdfg_node.sdfg.add_state_before(init_state, 'clean_symbols', is_start_state=True)
            #     edge = nsdfg_node.sdfg.edges_between(pre_init_state, init_state)[0]
            #     edge.data.assignments.update({str(k): str(v) for k, v in to_remove.items()})

        unsqueeze_info: Dict[str, Tuple[Dict[int, int], List[Any]]] = dict()

        for state, subgraph in subgraphs:
            components = MapFission._components(subgraph)
            sources = subgraph.source_nodes()
            sinks = subgraph.sink_nodes()

            # Collect external edges
            if self.expr_index == 0:
                external_edges_entry = list(state.out_edges(map_entry))
                external_edges_exit = list(state.in_edges(map_exit))
            else:
                external_edges_entry = [
                    e for e in subgraph.edges()
                    if (not e.data.is_empty() and isinstance(e.src, nodes.AccessNode) and not nsdfg_node.sdfg.arrays[e.src.data].transient)
                ]
                external_edges_exit = [
                    e for e in subgraph.edges()
                    if (not e.data.is_empty() and isinstance(e.dst, nodes.AccessNode) and not nsdfg_node.sdfg.arrays[e.dst.data].transient)
                ]

            # Map external edges to outer memlets
            edge_to_outer = {}
            for edge in external_edges_entry:
                if self.expr_index == 0:
                    # Subgraphs use the corresponding outer map edges
                    path = state.memlet_path(edge)
                    eindex = path.index(edge)
                    edge_to_outer[edge] = path[eindex - 1]
                else:
                    # Nested SDFGs use the internal map edges of the node
                    outer_edge = next(e for e in graph.in_edges(nsdfg_node) if e.dst_conn == edge.src.data)
                    edge_to_outer[edge] = outer_edge

            for edge in external_edges_exit:
                if self.expr_index == 0:
                    path = state.memlet_path(edge)
                    eindex = path.index(edge)
                    edge_to_outer[edge] = path[eindex + 1]
                else:
                    # Nested SDFGs use the internal map edges of the node
                    outer_edge = next(e for e in graph.out_edges(nsdfg_node) if e.src_conn == edge.dst.data)
                    edge_to_outer[edge] = outer_edge

            # Collect all border arrays and code->code edges
            arrays = MapFission._border_arrays(nsdfg_node.sdfg if self.expr_index == 1 else sdfg, state, subgraph)

            # Collect intermediate nodes in dataflow Map bodies
            intermediate_nodes = []
            if self.expr_index == 0:
                for node in subgraph.nodes():
                    if isinstance(node, nodes.AccessNode) and node.data in arrays:
                        intermediate_nodes.append(node)

            scalars = defaultdict(list)
            for _, component_out in components:
                for e in subgraph.out_edges(component_out):
                    if isinstance(e.dst, nodes.CodeNode):
                        scalars[e.data.data].append(e)

            # Create new arrays for scalars
            for scalar, edges in scalars.items():
                desc = parent.arrays[scalar]
                del parent.arrays[scalar]
                name, newdesc = parent.add_transient(scalar,
                                                     mapsize,
                                                     desc.dtype,
                                                     desc.storage,
                                                     lifetime=desc.lifetime,
                                                     debuginfo=desc.debuginfo,
                                                     allow_conflicts=desc.allow_conflicts,
                                                     find_new_name=True)

                # Add extra nodes in component boundaries
                for edge in edges:
                    anode = state.add_access(name)
                    sbs = subsets.Range.from_string(','.join(outer_map.params))
                    # Offset memlet by map range begin (to fit the transient)
                    sbs.offset([r[0] for r in outer_map.range], True)
                    state.add_edge(edge.src, edge.src_conn, anode, None,
                                   mm.Memlet.simple(name, sbs, num_accesses=outer_map.range.num_elements()))
                    state.add_edge(anode, None, edge.dst, edge.dst_conn,
                                   mm.Memlet.simple(name, sbs, num_accesses=outer_map.range.num_elements()))
                    state.remove_edge(edge)

            # Add extra maps around components
            new_map_entries = []
            for component_in, component_out in components:
                me, mx = state.add_map(outer_map.label + '_fission', [(p, '0:1') for p in outer_map.params],
                                       outer_map.schedule,
                                       unroll=outer_map.unroll,
                                       debuginfo=outer_map.debuginfo)

                # Add dynamic input connectors
                for conn in map_entry.in_connectors:
                    if not conn.startswith('IN_'):
                        me.add_in_connector(conn)

                me.map.range = dcpy(outer_map.range)
                new_map_entries.append(me)

                # Reconnect edges through new map
                conn_idx = 0
                for e in state.in_edges(component_in):
                    if e.data.data:
                        in_conn = f"IN_{conn_idx}"
                        out_conn = f"OUT_{conn_idx}"
                        conn_idx += 1
                        me.add_in_connector(in_conn)
                        me.add_out_connector(out_conn)
                    else:
                        in_conn = None
                        out_conn = None
                    state.add_edge(me, out_conn, e.dst, e.dst_conn, dcpy(e.data))
                    # Reconnect inner edges at source directly to external nodes
                    if self.expr_index == 0 and e in external_edges_entry:
                        state.add_edge(edge_to_outer[e].src, edge_to_outer[e].src_conn, me, in_conn,
                                       dcpy(edge_to_outer[e].data))
                    else:
                        state.add_edge(e.src, e.src_conn, me, in_conn, dcpy(e.data))
                    state.remove_edge(e)
                # Empty memlet edge in nested SDFGs
                if state.in_degree(component_in) == 0:
                    state.add_edge(me, None, component_in, None, mm.Memlet())

                conn_idx = 0
                for e in state.out_edges(component_out):
                    if e.data.data:
                        in_conn = f"IN_{conn_idx}"
                        out_conn = f"OUT_{conn_idx}"
                        conn_idx += 1
                        mx.add_in_connector(in_conn)
                        mx.add_out_connector(out_conn)
                    else:
                        in_conn = None
                        out_conn = None
                    state.add_edge(e.src, e.src_conn, mx, in_conn, dcpy(e.data))
                    # Reconnect inner edges at sink directly to external nodes
                    if self.expr_index == 0 and e in external_edges_exit:
                        state.add_edge(mx, out_conn, edge_to_outer[e].dst, edge_to_outer[e].dst_conn,
                                       dcpy(edge_to_outer[e].data))
                    else:
                        state.add_edge(mx, out_conn, e.dst, e.dst_conn, dcpy(e.data))
                    state.remove_edge(e)
                # Empty memlet edge in nested SDFGs
                if state.out_degree(component_out) == 0:
                    state.add_edge(component_out, None, mx, None, mm.Memlet())
            # Connect other sources/sinks not in components (access nodes)
            # directly to external nodes
            if self.expr_index == 0:
                for node in sources:
                    if isinstance(node, nodes.AccessNode):
                        for edge in state.in_edges(node):
                            outer_edge = edge_to_outer[edge]
                            memlet = dcpy(edge.data)
                            memlet.subset = subsets.Range(outer_map.range.ranges + memlet.subset.ranges)
                            state.add_edge(outer_edge.src, outer_edge.src_conn, edge.dst, edge.dst_conn, memlet)

                for node in sinks:
                    if isinstance(node, nodes.AccessNode):
                        for edge in state.out_edges(node):
                            outer_edge = edge_to_outer[edge]
                            state.add_edge(edge.src, edge.src_conn, outer_edge.dst, outer_edge.dst_conn,
                                           dcpy(outer_edge.data))

            # Augment arrays by prepending map dimensions
            for array in arrays:
                if array in modified_arrays:
                    continue
                desc = parent.arrays[array]
                if isinstance(desc, dt.Scalar):  # Scalar needs to be augmented to an array
                    desc = dt.Array(desc.dtype, desc.shape, desc.transient, desc.allow_conflicts, desc.storage,
                                    desc.location, desc.strides, desc.offset, False, desc.lifetime, 0, desc.debuginfo,
                                    desc.total_size, desc.start_offset)
                    parent.arrays[array] = desc
                for sz in reversed(mapsize):
                    desc.strides = [desc.total_size] + list(desc.strides)
                    desc.total_size = desc.total_size * sz

                desc.shape = mapsize + list(desc.shape)
                # Try to keep consistent offsets.
                offset = desc.offset[0]
                if any(o != offset for o in desc.offset):
                    offset = 0
                desc.offset = [offset] * len(mapsize) + list(desc.offset)
                modified_arrays.add(array)

            # Fill scope connectors so that memlets can be tracked below
            state.fill_scope_connectors()

            # Correct connectors and memlets in nested SDFGs to account for
            # missing outside map
            if self.expr_index == 1:

                # NOTE: In the following scope dictionary, we mark the new MapEntries as existing in their own scope.
                # This makes it easier to detect edges that are outside the new Map scopes (after MapFission).
                scope_dict = state.scope_dict()
                for k, v in scope_dict.items():
                    if isinstance(k, nodes.MapEntry) and k in new_map_entries and v is None:
                        scope_dict[k] = k

                to_correct = ([(e, e.src) for e in external_edges_entry] + [(e, e.dst) for e in external_edges_exit])

                # NOTE: There can be multiple nodes to the same data. Therefore, we need to ensure that we update their
                # descriptors only once. We need to also ensure that we keep track of the indices needed for
                # unsqueezing their memlets

                # `unsqueeze_info` match the name of an inner descriptor with (1) a dictionary matching its extents to
                # those of the corresponding outer descriptor, and (2) a template range that can be used to unsqueeze
                # the inner memlets.
                # unsqueeze_info: Dict[str, Tuple[Dict[int, int], List[Any]]] = dict()

                for edge, node in to_correct:
                    if node.data in unsqueeze_info:
                        continue

                    outer_edge = edge_to_outer[edge]
                    desc = parent.arrays[node.data]

                    # Modify shape of internal array to match outer one
                    outer_desc = sdfg.arrays[outer_edge.data.data]

                    # Find the extra dimensions in the outer array
                    # NOTE: We assume that inner array is a subset of outer array, i.e., there are not extra
                    # unitary dimensions in the inner array.
                    # NOTE: Extents (lengths of dimensions) can be deceptive. It is better to use strides.
                    common_dims = dict()
                    exclusive_inner_dims = []
                    exclusive_outer_dims = set(range(len(outer_desc.shape)))
                    for i, inner_stride in enumerate(desc.strides):
                        try:
                            inner_extent = desc.shape[i]
                            extents_match = False
                            start = 0
                            while not extents_match:
                                j = outer_desc.strides.index(inner_stride, start)
                                outer_extent = outer_desc.shape[j]
                                extents_match = (inner_extent == outer_extent)
                                start = j + 1
                            common_dims[i] = j
                            exclusive_outer_dims.remove(j)
                        except ValueError:
                            exclusive_inner_dims.append(i)
                    assert len(common_dims) == len(desc.shape) - len(exclusive_inner_dims)

                    map_params = list(map_entry.map.params)
                    map_ranges = list(map_entry.map.range.ranges)
                    exclusive_outer_dims = list(sorted(exclusive_outer_dims))
                    template_rng = []
                    for i in range(len(outer_desc.shape)):
                        if i in common_dims.values():
                            template_rng.append(None)
                        # elif map_params:
                        #     param = map_params.pop(0)
                        #     rng = map_ranges.pop(0)
                        #     template_rng.append((f"{param} - {rng[0]}", f"{param} - {rng[0]}", 1))
                        elif i in exclusive_outer_dims:
                            # template_rng.append((0, outer_desc.shape[i] - 1
                            # template_rng.append((0, 0, 1))
                            template_rng.append(outer_edge.data.subset[i])
                            warnings.warn(f"MapFission: Added range from outer subset ({outer_edge.data.subset[i]}) to {node.data} for dimension {i}")
                        else:
                            raise NotImplementedError
                    
                    unsqueeze_info[node.data] = (common_dims, template_rng)

                    if isinstance(desc, dt.Scalar):
                        parent.arrays[node.data] = dcpy(outer_desc)
                        desc = parent.arrays[node.data]
                        desc.transient = False
                    elif isinstance(desc, dt.Array):
                        desc.shape = outer_desc.shape
                        desc.strides = outer_desc.strides
                        desc.total_size = outer_desc.total_size
                    
                    if isinstance(desc, dt.Array):
                        desc.offset = outer_desc.offset


                corrected_nodes = set()
                corrected_edges = set()
                for edge, node in to_correct:
                    if isinstance(node, nodes.AccessNode):
                        if node in corrected_nodes:
                            continue
                        corrected_nodes.add(node)

                        # # Get Map
                        # if isinstance(outer_edge.src, nodes.MapEntry):
                        #     scope_map = outer_edge.src.map
                        # elif isinstance(outer_edge.dst, nodes.MapExit):
                        #     scope_map = outer_edge.dst.map
                        # else:
                        #     scope_map = None

                        desc = parent.arrays[node.data]
                        common_dims, template_rng = unsqueeze_info[node.data]

                        # Inside the nested SDFG, offset all memlets to include
                        # the offsets from within the map.
                        # NOTE: Relies on propagation to fix outer memlets
                        for internal_edge in state.all_edges(node):
                            for e in state.memlet_tree(internal_edge):
                                if e in corrected_edges:
                                    continue
                                corrected_edges.add(e)
                                if e.data.is_empty():
                                    continue
                                subset = e.data.subset if e.data.data == node.data else e.data.other_subset
                                new_subset = dcpy(template_rng)
                                for i, j in common_dims.items():
                                    new_subset[j] = subset[i]
                                new_subset = subsets.Range(new_subset)
                                if e.data.data == node.data:
                                    e.data.subset = new_subset
                                else:
                                    e.data.other_subset = new_subset
                                # e.data.subset = helpers.unsqueeze_memlet(e.data,
                                #                                          outer_edge.data,
                                #                                          internal_offset=desc.offset,
                                #                                          external_offset=outer_desc.offset,
                                #                                          map=scope_map).subset
                                # NOTE: If the edge is outside of the new Map scope, then try to propagate it. This is
                                # needed for edges directly connecting AccessNodes, because the standard memlet
                                # propagation will stop at the first AccessNode outside the Map scope. For example, see
                                # `test.transformations.mapfission_test.MapFissionTest.test_array_copy_outside_scope`.
                                if not (scope_dict[e.src] and scope_dict[e.dst]):
                                    e.data = propagate_subset([e.data], desc, outer_map.params, outer_map.range)

                        # # Only after offsetting memlets we can modify the overall offset
                        # if isinstance(desc, dt.Array):
                        #     desc.offset = outer_desc.offset

            # Fill in memlet trees for border transients
            # NOTE: Memlet propagation should run to correct the outer edges
            for node in subgraph.nodes():
                if isinstance(node, nodes.AccessNode) and node.data in arrays:
                    offsets = parent.arrays[node.data].offset[:len(outer_map.params)]
                    new_ranges = [(pystr_to_symbolic(d) - r[0] - o, pystr_to_symbolic(d) - r[0] - o, 1)
                                  for d, r, o in zip(outer_map.params, outer_map.range, offsets)]
                    for edge in state.all_edges(node):
                        for e in state.memlet_tree(edge):
                            if e.data.is_empty():
                                continue
                            # Prepend map dimensions to memlet
                            # NOTE: Do this only for the subset corresponding to `node.data`. If the edge is copying
                            # to/from another AccessNode, the other data may not need extra dimensions. For example, see
                            # `test.transformations.mapfission_test.MapFissionTest.test_array_copy_outside_scope`.
                            if e.data.data == node.data:
                                if e.data.subset:
                                    e.data.subset = subsets.Range(new_ranges + e.data.subset.ranges)
                            else:
                                if e.data.other_subset:
                                    e.data.other_subset = subsets.Range(new_ranges + e.data.other_subset.ranges)

        # If nested SDFG, reconnect nodes around map and modify memlets
        if self.expr_index == 1:
            for edge in graph.in_edges(map_entry):
                if not edge.dst_conn or not edge.dst_conn.startswith('IN_'):
                    continue

                # Modify edge coming into nested SDFG to include entire array
                desc = sdfg.arrays[edge.data.data]
                edge.data.subset = subsets.Range.from_array(desc)
                edge.data.num_accesses = edge.data.subset.num_elements()

                # Find matching edge inside map
                for inner_edge in graph.out_edges_by_connector(map_entry, f"OUT_{edge.dst_conn[3:]}"):
                    graph.add_edge(edge.src, edge.src_conn, nsdfg_node, inner_edge.dst_conn, dcpy(edge.data))

            for edge in graph.out_edges(map_exit):
                # Modify edge coming out of nested SDFG to include entire array
                desc = sdfg.arrays[edge.data.data]
                edge.data.subset = subsets.Range.from_array(desc)

                # Find matching edge inside map
                for inner_edge in graph.in_edges_by_connector(map_exit, f"IN_{edge.src_conn[4:]}"):
                    graph.add_edge(nsdfg_node, inner_edge.src_conn, edge.dst, edge.dst_conn, dcpy(edge.data))
        else:
            # In dataflow bodies, reconnect intermediate nodes to subgraphs outside of the (original) Map
            for node in intermediate_nodes:
                full_memlet = mm.Memlet.from_array(node.data, sdfg.arrays[node.data])
                for edge in graph.edges_between(map_entry, node):
                    path = graph.memlet_path(edge)
                    if len(path) > 1:
                        outer_edge = path[-2]
                        src_subset = outer_edge.data.get_src_subset(outer_edge, graph)
                        dst_subset = full_memlet.subset.compose(src_subset)
                        mem = mm.Memlet(data=outer_edge.data.data, subset=src_subset, other_subset=dst_subset)
                        graph.add_edge(outer_edge.src, outer_edge.src_conn, node, edge.dst_conn, mem)
                for edge in graph.edges_between(node, map_exit):
                    path = graph.memlet_path(edge)
                    if len(path) > 1:
                        outer_edge = path[1]
                        dst_subset = outer_edge.data.get_dst_subset(outer_edge, graph)
                        src_subset = full_memlet.subset.compose(dst_subset)
                        mem = mm.Memlet(data=outer_edge.data.data, subset=dst_subset, other_subset=src_subset)
                        graph.add_edge(node, edge.src_conn, outer_edge.dst, outer_edge.dst_conn, mem)

        # Remove outer map
        graph.remove_nodes_from([map_entry, map_exit])

        # NOTE: It is better to manually call memlet propagation here to ensure that all subsets are properly updated.
        # This can solve issues when, e.g., applying MapFission through `SDFG.apply_transformations_repeated`.
        propagate_memlets_state(sdfg, graph)
