# Copyright 2019-2022 ETH Zurich and the DaCe authors. All rights reserved.
import copy
from dataclasses import dataclass, field
from dace import nodes, data, subsets
from dace.codegen import control_flow as cf
from dace.dtypes import TYPECLASS_TO_STRING
from dace.properties import CodeBlock
from dace.sdfg import SDFG, InterstateEdge
from dace.sdfg.state import SDFGState
from dace.symbolic import symbol
from dace.memlet import Memlet
from functools import reduce
from typing import Dict, List, Optional, Set, Tuple, Union

INDENTATION = '  '


class UnsupportedScopeException(Exception):
    pass


@dataclass
class ScheduleTreeNode:
    # sdfg: SDFG
    # sdfg: Optional[SDFG] = field(default=None, init=False)
    parent: Optional['ScheduleTreeScope'] = field(default=None, init=False)

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + 'UNSUPPORTED'
    
    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None) -> Tuple[str, Set[str]]:
        string, defined_arrays = self.define_arrays(indent, defined_arrays)
        return string + indent * INDENTATION + 'pass', defined_arrays
    
    def define_arrays(self, indent: int, defined_arrays: Set[str]) -> Tuple[str, Set[str]]:
        return '', defined_arrays
        # defined_arrays = defined_arrays or set()
        # string = ''
        # undefined_arrays = {name: desc for name, desc in self.sdfg.arrays.items() if not name in defined_arrays and desc.transient}
        # if hasattr(self, 'children'):
        #     times_used = {name: 0 for name in undefined_arrays}
        #     for child in self.children:
        #         for name in undefined_arrays:
        #             if child.is_data_used(name):
        #                 times_used[name] += 1
        #     undefined_arrays = {name: desc for name, desc in undefined_arrays.items() if times_used[name] > 1}
        # for name, desc in undefined_arrays.items():
        #     string += indent * INDENTATION + f"{name} = numpy.ndarray({desc.shape}, {TYPECLASS_TO_STRING[desc.dtype].replace('::', '.')})\n"
        # defined_arrays |= undefined_arrays.keys()
        # return string, defined_arrays
    
    def is_data_used(self, name: str, include_symbols: bool = False) -> bool:
        pass
        # for child in self.children:
        #     if child.is_data_used(name):
        #         return True
        # return False


@dataclass
class ScheduleTreeScope(ScheduleTreeNode):
    sdfg: SDFG
    top_level: bool
    children: List['ScheduleTreeNode']
    containers: Optional[Dict[str, data.Data]] = field(default_factory=dict, init=False)
    symbols: Optional[Dict[str, symbol]] = field(default_factory=dict, init=False)

    # def __init__(self, sdfg: Optional[SDFG] = None, top_level: Optional[bool] = False, children: Optional[List['ScheduleTreeNode']] = None):
    def __init__(self, sdfg: Optional[SDFG] = None, top_level: Optional[bool] = False, children: Optional[List['ScheduleTreeNode']] = None):
        self.sdfg = sdfg
        self.top_level = top_level
        self.children = children or []
        if self.children:
            for child in children:
                child.parent = self
        # self.__post_init__()
        # for child in children:
        #     child.parent = self
        # _, defined_arrays = self.define_arrays(0, set())
        # self.containers = {name: copy.deepcopy(sdfg.arrays[name]) for name in defined_arrays}
        # if top_level:
        #     self.containers.update({name: copy.deepcopy(desc) for name, desc in sdfg.arrays.items() if not desc.transient})
        # # self.containers = {name: copy.deepcopy(container) for name, container in sdfg.arrays.items()}
    
    # def __post_init__(self):
    #     for child in self.children:
    #         child.parent = self
    #     _, defined_arrays = self.define_arrays(0, set())
    #     self.containers = {name: copy.deepcopy(self.sdfg.arrays[name]) for name in defined_arrays}
    #     if self.top_level:
    #         self.containers.update({name: copy.deepcopy(desc) for name, desc in self.sdfg.arrays.items() if not desc.transient})
    #     # self.containers = {name: copy.deepcopy(container) for name, container in sdfg.arrays.items()}

    def as_string(self, indent: int = 0):
        return '\n'.join([child.as_string(indent + 1) for child in self.children])
    
    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None, def_offset: int = 1, sep_defs: bool = False) -> Tuple[str, Set[str]]:
        if self.top_level:
            header = ''
            for s in self.sdfg.free_symbols:
                header += f"{s} = dace.symbol('{s}', {TYPECLASS_TO_STRING[self.sdfg.symbols[s]].replace('::', '.')})\n"
            header += f"""
@dace.program
def {self.sdfg.label}({self.sdfg.python_signature()}):
"""
            # defined_arrays = set([name for name, desc in self.sdfg.arrays.items() if not desc.transient])
            defined_arrays = set([name for name, desc in self.containers.items() if not desc.transient])        
        else:
            header = ''
            defined_arrays = defined_arrays or set()
        cindent = indent + def_offset
        # string, defined_arrays = self.define_arrays(indent + 1, defined_arrays)
        definitions = ''
        body = ''
        undefined_arrays = {name: desc for name, desc in self.containers.items() if name not in defined_arrays}
        for name, desc in undefined_arrays.items():
            if isinstance(desc, data.Scalar):
                definitions += cindent * INDENTATION + f"{name} = numpy.{desc.dtype.as_numpy_dtype()}(0)\n"
            else:
                definitions += cindent * INDENTATION + f"{name} = numpy.ndarray({desc.shape}, {TYPECLASS_TO_STRING[desc.dtype].replace('::', '.')})\n"
        defined_arrays |= undefined_arrays.keys()
        for child in self.children:
            substring, defined_arrays = child.as_python(indent + 1, defined_arrays)
            body += substring
            if body[-1] != '\n':
                body += '\n'
        if sep_defs:
            return definitions, body, defined_arrays
        else:
            return header + definitions + body, defined_arrays
    
    def define_arrays(self, indent: int, defined_arrays: Set[str]) -> Tuple[str, Set[str]]:
        defined_arrays = defined_arrays or set()
        string = ''
        undefined_arrays = {}
        for sdfg in self.sdfg.all_sdfgs_recursive():
            undefined_arrays.update({name: desc for name, desc in sdfg.arrays.items() if not name in defined_arrays and desc.transient})
        # undefined_arrays = {name: desc for name, desc in self.sdfg.arrays.items() if not name in defined_arrays and desc.transient}
        times_used = {name: 0 for name in undefined_arrays}
        for child in self.children:
            for name in undefined_arrays:
                if child.is_data_used(name):
                    times_used[name] += 1
        undefined_arrays = {name: desc for name, desc in undefined_arrays.items() if times_used[name] > 1}
        if not self.containers:
            self.containers = {}
        for name, desc in undefined_arrays.items():
            string += indent * INDENTATION + f"{name} = numpy.ndarray({desc.shape}, {TYPECLASS_TO_STRING[desc.dtype].replace('::', '.')})\n"
            self.containers[name] = copy.deepcopy(desc)
        defined_arrays |= undefined_arrays.keys()
        return string, defined_arrays
    
    def is_data_used(self, name: str) -> bool:
        for child in self.children:
            if child.is_data_used(name):
                return True
        return False

    # TODO: Get input/output memlets?


@dataclass
class ControlFlowScope(ScheduleTreeScope):
    pass


@dataclass
class DataflowScope(ScheduleTreeScope):
    node: nodes.EntryNode


@dataclass
class GBlock(ControlFlowScope):
    """
    General control flow block. Contains a list of states
    that can run in arbitrary order based on edges (gotos).
    Normally contains irreducible control flow.
    """

    def as_string(self, indent: int = 0):
        result = indent * INDENTATION + 'gblock:\n'
        return result + super().as_string(indent)

    pass


@dataclass
class StateLabel(ScheduleTreeNode):
    state: SDFGState

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + f'label {self.state.name}:'


@dataclass
class GotoNode(ScheduleTreeNode):
    target: Optional[str] = None  #: If None, equivalent to "goto exit" or "return"

    def as_string(self, indent: int = 0):
        name = self.target or 'exit'
        return indent * INDENTATION + f'goto {name}'


@dataclass
class AssignNode(ScheduleTreeNode):
    """
    Represents a symbol assignment that is not part of a structured control flow block.
    """
    name: str
    value: CodeBlock
    edge: InterstateEdge

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + f'assign {self.name} = {self.value.as_string}'


@dataclass
class ForScope(ControlFlowScope):
    """
    For loop scope.
    """
    header: cf.ForScope

    def as_string(self, indent: int = 0):
        node = self.header

        result = (indent * INDENTATION + f'for {node.itervar} = {node.init}; {node.condition.as_string}; '
                  f'{node.itervar} = {node.update}:\n')
        return result + super().as_string(indent)
    
    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None) -> Tuple[str, Set[str]]:
        node = self.header
        result = indent * INDENTATION + f'{node.itervar} = {node.init}\n'
        result += indent * INDENTATION + f'while {node.condition.as_string}:\n'
        defs, body, defined_arrays = super().as_python(indent, defined_arrays, def_offset=0, sep_defs=True)
        result = defs + result + body
        result += (indent + 1) * INDENTATION + f'{node.itervar} = {node.update}\n'
        return result, defined_arrays


@dataclass
class WhileScope(ControlFlowScope):
    """
    While loop scope.
    """
    header: cf.WhileScope

    def as_string(self, indent: int = 0):
        result = indent * INDENTATION + f'while {self.header.test.as_string}:\n'
        return result + super().as_string(indent)


@dataclass
class DoWhileScope(ControlFlowScope):
    """
    Do/While loop scope.
    """
    header: cf.DoWhileScope

    def as_string(self, indent: int = 0):
        header = indent * INDENTATION + 'do:\n'
        footer = indent * INDENTATION + f'while {self.header.test.as_string}\n'
        return header + super().as_string(indent) + footer


@dataclass
class IfScope(ControlFlowScope):
    """
    If branch scope.
    """
    condition: CodeBlock

    def as_string(self, indent: int = 0):
        result = indent * INDENTATION + f'if {self.condition.as_string}:\n'
        return result + super().as_string(indent)
    
    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None) -> Tuple[str, Set[str]]:
        result = indent * INDENTATION + f'if {self.condition.as_string}:\n'
        string, defined_arrays = super().as_python(indent, defined_arrays)
        return result + string, defined_arrays
    
    def is_data_used(self, name: str) -> bool:
        result = name in self.condition.get_free_symbols()
        result |= super().is_data_used(name)
        return result


@dataclass
class StateIfScope(IfScope):
    """
    A special class of an if scope in general blocks for if statements that are part of a state transition.
    """

    def as_string(self, indent: int = 0):
        result = indent * INDENTATION + f'stateif {self.condition.as_string}:\n'
        return result + super().as_string(indent)


@dataclass
class BreakNode(ScheduleTreeNode):
    """
    Represents a break statement.
    """

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + 'break'


@dataclass
class ContinueNode(ScheduleTreeNode):
    """
    Represents a continue statement.
    """

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + 'continue'


@dataclass
class ElifScope(ControlFlowScope):
    """
    Else-if branch scope.
    """
    condition: CodeBlock

    def as_string(self, indent: int = 0):
        result = indent * INDENTATION + f'elif {self.condition.as_string}:\n'
        return result + super().as_string(indent)


@dataclass
class ElseScope(ControlFlowScope):
    """
    Else branch scope.
    """

    def as_string(self, indent: int = 0):
        result = indent * INDENTATION + 'else:\n'
        return result + super().as_string(indent)


@dataclass
class MapScope(DataflowScope):
    """
    Map scope.
    """

    def as_string(self, indent: int = 0):
        rangestr = ', '.join(subsets.Range.dim_to_string(d) for d in self.node.map.range)
        result = indent * INDENTATION + f'map {", ".join(self.node.map.params)} in [{rangestr}]:\n'
        return result + super().as_string(indent)

    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None) -> Tuple[str, Set[str]]:
        rangestr = ', '.join(subsets.Range.dim_to_string(d) for d in self.node.map.range)
        result = indent * INDENTATION + f'for {", ".join(self.node.map.params)} in dace.map[{rangestr}]:\n'
        string, defined_arrays = super().as_python(indent, defined_arrays)
        return result + string, defined_arrays


@dataclass
class ConsumeScope(DataflowScope):
    """
    Consume scope.
    """

    def as_string(self, indent: int = 0):
        node: nodes.ConsumeEntry = self.node
        cond = 'stream not empty' if node.consume.condition is None else node.consume.condition.as_string
        result = indent * INDENTATION + f'consume (PE {node.consume.pe_index} out of {node.consume.num_pes}) while {cond}:\n'
        return result + super().as_string(indent)


@dataclass
class PipelineScope(DataflowScope):
    """
    Pipeline scope.
    """

    def as_string(self, indent: int = 0):
        rangestr = ', '.join(subsets.Range.dim_to_string(d) for d in self.node.map.range)
        result = indent * INDENTATION + f'pipeline {", ".join(self.node.map.params)} in [{rangestr}]:\n'
        return result + super().as_string(indent)


def _memlet_to_str(memlet: Memlet) -> str:
    assert memlet.other_subset == None
    wcr = ""
    if memlet.wcr:
        wcr = f"({reduce(lambda x, y: x * y, memlet.subset.size())}, {memlet.wcr})"
    return f"{memlet.data}{wcr}[{memlet.subset}]"


@dataclass
class TaskletNode(ScheduleTreeNode):
    node: nodes.Tasklet
    in_memlets: Dict[str, Memlet]
    out_memlets: Dict[str, Memlet]

    def as_string(self, indent: int = 0):
        in_memlets = ', '.join(f'{v}' for v in self.in_memlets.values())
        out_memlets = ', '.join(f'{v}' for v in self.out_memlets.values())
        return indent * INDENTATION + f'{out_memlets} = tasklet({in_memlets})'

    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None) -> Tuple[str, Set[str]]:
        explicit_dataflow = indent * INDENTATION + "with dace.tasklet:\n"
        for conn, memlet in self.in_memlets.items():
            explicit_dataflow += (indent + 1) * INDENTATION + f"{conn} << {_memlet_to_str(memlet)}\n"
        for conn, memlet in self.out_memlets.items():
            explicit_dataflow += (indent + 1) * INDENTATION + f"{conn} >> {_memlet_to_str(memlet)}\n"
        code = self.node.code.as_string.replace('\n', f"\n{(indent + 1) * INDENTATION}")
        explicit_dataflow += (indent + 1) * INDENTATION + code
        defined_arrays = defined_arrays or set()
        string, defined_arrays = self.define_arrays(indent, defined_arrays)
        return string + explicit_dataflow, defined_arrays

    def is_data_used(self, name: str, include_symbols: bool = False) -> bool:
        used_data = set([memlet.data for memlet in self.in_memlets.values()])
        used_data |= set([memlet.data for memlet in self.out_memlets.values()])
        if include_symbols:
            for memlet in self.in_memlets.values():
                used_data |= memlet.subset.free_symbols
                if memlet.other_subset:
                    used_data |= memlet.other_subset.free_symbols
        return name in used_data


@dataclass
class LibraryCall(ScheduleTreeNode):
    node: nodes.LibraryNode
    in_memlets: Union[Dict[str, Memlet], Set[Memlet]]
    out_memlets: Union[Dict[str, Memlet], Set[Memlet]]

    def as_string(self, indent: int = 0):
        if isinstance(self.in_memlets, set):
            in_memlets = ', '.join(f'{v}' for v in self.in_memlets)
        else:
            in_memlets = ', '.join(f'{v}' for v in self.in_memlets.values())
        if isinstance(self.out_memlets, set):
            out_memlets = ', '.join(f'{v}' for v in self.out_memlets)
        else:
            out_memlets = ', '.join(f'{v}' for v in self.out_memlets.values())
        libname = type(self.node).__name__
        # Get the properties of the library node without its superclasses
        own_properties = ', '.join(f'{k}={getattr(self.node, k)}' for k, v in self.node.__properties__.items()
                                   if v.owner not in {nodes.Node, nodes.CodeNode, nodes.LibraryNode})
        return indent * INDENTATION + f'{out_memlets} = library {libname}[{own_properties}]({in_memlets})'

    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None) -> Tuple[str, Set[str]]:
        if isinstance(self.in_memlets, set):
            in_memlets = ', '.join(f'{v}' for v in self.in_memlets)
        else:
            in_memlets = ', '.join(f"'{k}': {v}" for k, v in self.in_memlets.items())
        if isinstance(self.out_memlets, set):
            out_memlets = ', '.join(f'{v}' for v in self.out_memlets)
        else:
            out_memlets = ', '.join(f"'{k}': {v}" for k, v in self.out_memlets.items())
        libname = type(self.node).__module__ + '.' + type(self.node).__qualname__
        # Get the properties of the library node without its superclasses
        own_properties = ', '.join(f'{k}={getattr(self.node, k)}' for k, v in self.node.__properties__.items()
                                   if v.owner not in {nodes.Node, nodes.CodeNode, nodes.LibraryNode})
        defined_arrays = defined_arrays or set()
        string, defined_arrays = self.define_arrays(indent, defined_arrays)
        return string + indent * INDENTATION + f"dace.tree.library(ltype={libname}, label='{self.node.label}', inputs={{{in_memlets}}}, outputs={{{out_memlets}}}, {own_properties})", defined_arrays

    def is_data_used(self, name: str) -> bool:
        if isinstance(self.in_memlets, set):
            used_data = set([memlet.data for memlet in self.in_memlets])
        else:
            used_data = set([memlet.data for memlet in self.in_memlets.values()])
        if isinstance(self.out_memlets, set):
            used_data |= set([memlet.data for memlet in self.out_memlets])
        else:
            used_data |= set([memlet.data for memlet in self.out_memlets.values()])
        return name in used_data


@dataclass
class CopyNode(ScheduleTreeNode):
    target: str
    memlet: Memlet

    def as_string(self, indent: int = 0):
        if self.memlet.other_subset is not None and any(s != 0 for s in self.memlet.other_subset.min_element()):
            offset = f'[{self.memlet.other_subset}]'
        else:
            offset = ''
        if self.memlet.wcr is not None:
            wcr = f' with {self.memlet.wcr}'
        else:
            wcr = ''

        return indent * INDENTATION + f'{self.target}{offset} = copy {self.memlet.data}[{self.memlet.subset}]{wcr}'
        
    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None) -> Tuple[str, Set[str]]:
        if self.memlet.other_subset is not None and any(s != 0 for s in self.memlet.other_subset.min_element()):
            offset = f'[{self.memlet.other_subset}]'
        else:
            offset = f'[{self.memlet.subset}]'
        if self.memlet.wcr is not None:
            wcr = f' with {self.memlet.wcr}'
        else:
            wcr = ''

        defined_arrays = defined_arrays or set()
        string, defined_arrays = self.define_arrays(indent, defined_arrays)
        return string + indent * INDENTATION + f'dace.tree.copy(src={self.memlet.data}[{self.memlet.subset}], dst={self.target}{offset}, wcr={self.memlet.wcr})', defined_arrays

    def is_data_used(self, name: str) -> bool:
        return name is self.memlet.data or name is self.target


@dataclass
class DynScopeCopyNode(ScheduleTreeNode):
    """
    A special case of a copy node that is used in dynamic scope inputs (e.g., dynamic map ranges).
    """
    target: str
    memlet: Memlet

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + f'{self.target} = dscopy {self.memlet.data}[{self.memlet.subset}]'


@dataclass
class ViewNode(ScheduleTreeNode):
    target: str  #: View name
    source: str  #: Viewed container name
    memlet: Memlet
    src_desc: data.Data
    view_desc: data.Data

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + f'{self.target} = view {self.memlet} as {self.view_desc.shape}'
    
    def as_python(self, indent: int = 0, defined_arrays: Set[str] = None) -> Tuple[str, Set[str]]:
        defined_arrays = defined_arrays or set()
        string, defined_arrays = self.define_arrays(indent, defined_arrays)
        return string + indent * INDENTATION + f"{self.target} = {self.memlet}", defined_arrays
    
    def is_data_used(self, name: str) -> bool:
        # NOTE: View data must not be considered used
        return name is self.memlet.data


@dataclass
class NView(ViewNode):
    """
    Nested SDFG view node. Subclass of a view that specializes in nested SDFG boundaries.
    """

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + f'{self.target} = nview {self.memlet} as {self.view_desc.shape}'


@dataclass
class RefSetNode(ScheduleTreeNode):
    """
    Reference set node. Sets a reference to a data container.
    """
    target: str
    memlet: Memlet
    src_desc: data.Data
    ref_desc: data.Data

    def as_string(self, indent: int = 0):
        return indent * INDENTATION + f'{self.target} = refset to {self.memlet}'


# Classes based on Python's AST NodeVisitor/NodeTransformer for schedule tree nodes
class ScheduleNodeVisitor:

    def visit(self, node: ScheduleTreeNode):
        """Visit a node."""
        if isinstance(node, list):
            return [self.visit(snode) for snode in node]

        method = 'visit_' + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        return visitor(node)

    def generic_visit(self, node: ScheduleTreeNode):
        if isinstance(node, ScheduleTreeScope):
            for child in node.children:
                self.visit(child)


class ScheduleNodeTransformer(ScheduleNodeVisitor):

    def visit(self, node: ScheduleTreeNode):
        if isinstance(node, list):
            result = []
            for snode in node:
                new_node = self.visit(snode)
                if new_node is not None:
                    result.append(new_node)
            return result

        return super().visit(node)

    def generic_visit(self, node: ScheduleTreeNode):
        new_values = []
        if isinstance(node, ScheduleTreeScope):
            for value in node.children:
                if isinstance(value, ScheduleTreeNode):
                    value = self.visit(value)
                    if value is None:
                        continue
                    elif not isinstance(value, ScheduleTreeNode):
                        new_values.extend(value)
                        continue
                new_values.append(value)
            for val in new_values:
                val.parent = node
            node.children[:] = new_values
        return node
