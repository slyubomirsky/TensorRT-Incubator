#
# SPDX-FileCopyrightText: Copyright (c) 1993-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from typing import Dict, List, Sequence, Set

from tripy import utils
from tripy.common.shape_bounds import ShapeBounds


class FlatIR:
    """
    A flattened low level representation of a computation graph which maps directly with StableHLO dialect.
    """

    def __init__(self, shapes: Sequence[ShapeBounds] = None):
        self.inputs: List["FlatIRTensor"] = []
        self.outputs: List["FlatIRTensor"] = []
        self.ops: List["BaseFlatIROp"] = []

        self.shapes = shapes
        self.tensor_map: Dict[str] = {}

    def __str__(self):
        layer_strs: List[str] = []
        if len(self.inputs):
            layer_strs.append("inputs:")
        for inp in self.inputs:
            layer_strs.append(f"    {str(inp)}")
        for op in self.ops:
            layer_strs.append(str(op))
        layer_strs.append("outputs:")
        for out in self.outputs:
            layer_strs.append(f"    {str(out)}")
        return "\n".join(layer_strs)

    def to_mlir(self):
        def to_mlir_impl():
            from mlir_tensorrt.compiler import ir
            from mlir_tensorrt.compiler.dialects import func as func_dialect

            from tripy.backend.mlir.utils import make_ir_context, make_tensor_location

            with make_ir_context(), ir.Location.unknown():
                module = ir.Module.create()
                with ir.InsertionPoint(module.body) as ip:
                    # Lets assume only one function with inline code (#9 will fix it)
                    inp_types = [inp.to_mlir() for inp in self.inputs]
                    out_types = [o.to_mlir() for o in self.outputs]
                    ftype = ir.FunctionType.get(inp_types, out_types)
                    # TODO: Function name should be a property of Trace and used here.
                    func_op = func_dialect.FuncOp("main", ftype, ip=ip)
                    entry_block = func_op.add_entry_block()
                    with ir.InsertionPoint(entry_block):
                        ops = []
                        mlir_ops: Dict[str, ir.BlockArgument] = {}
                        # Initialize tensor dict with inputs
                        for index, inp in enumerate(self.inputs):
                            mlir_ops[inp.name] = entry_block.arguments[index]

                        for op in self.ops:
                            layer_inputs = [mlir_ops[inp.name] for inp in op.inputs]

                            with make_tensor_location(
                                [inp.name for inp in op.inputs],
                                [out.name for out in op.outputs],
                                op.trace_input_names,
                                op.trace_output_names,
                            ):
                                layer_outputs = op.to_mlir(layer_inputs)

                                # stablehlo python bindings can do some naive shape and type inference.
                                # If the shapes are freezed after adding a layer, assign these shapes back to flat_ir tensor.
                                for mlir_out, flatir_out in zip(layer_outputs, op.outputs):
                                    assert hasattr(mlir_out, "type") or hasattr(mlir_out, "result")
                                    type = mlir_out.type if hasattr(mlir_out, "type") else mlir_out.result.type
                                    flatir_out.shape = tuple(
                                        [
                                            (-1 if type.is_dynamic_dim(i) else type.get_dim_size(i))
                                            for i in range(type.rank)
                                        ]
                                    )

                            ops.extend(layer_outputs)
                            mlir_ops.update(zip([out.name for out in op.outputs], layer_outputs))

                        func_dialect.ReturnOp([mlir_ops[o.name] for o in self.outputs])

                    # After lowering the complete graph to stablehlo, there can be mismatch between Tripy created function signature and the ReturnOp due to shapes that resolved while lowering into Stablehlo.
                    # Here, we check if the types for the results and change the function signature to obey the inferred types.
                    new_out_types = [
                        mlir_ops[o.name].type if hasattr(mlir_ops[o.name], "type") else mlir_ops[o.name].result.type
                        for o in self.outputs
                    ]
                    ftype = ir.FunctionType.get(inp_types, new_out_types)
                    func_op.attributes["function_type"] = ir.TypeAttr.get(ftype)

                    if self.shapes:
                        # Create tensorrt.shape_profile attribute for all function arguments
                        arg_attrs: List[Dict[str, ir.Attribute]] = []
                        for bound in self.shapes:
                            # TODO (#244): Support multiple profiles

                            arg_attrs.append(
                                {
                                    "tensorrt.shape_profile": ir.Attribute.parse(
                                        f"#tensorrt.shape_profile<min={list(bound.min)}, opt={list(bound.opt)}, max={list(bound.max)}>"
                                    )
                                }
                            )

                        func_op.arg_attrs = ir.ArrayAttr.get([ir.DictAttr.get(attrs) for attrs in arg_attrs])

                    # Append device location if outputs are on host as MLIR-TensorRT does not adhere to this constraint.
                    # TODO(#155): Fix TensorKindAnalysis to ensure result tensors with attribute `tensorrt.host_tensor` are allocated on host.
                    res_attrs = []
                    for output in self.outputs:
                        if output.device.kind == "cpu":
                            res_attrs.append(ir.Attribute.parse("{tensorrt.host_tensor}"))
                        else:
                            res_attrs.append(ir.DictAttr.get({}))
                    func_op.res_attrs = ir.ArrayAttr.get(res_attrs)

                module.operation.attributes["sym_name"] = ir.StringAttr.get(
                    utils.UniqueNameGen.gen_uid([inp.name for inp in self.inputs], [out.name for out in self.outputs])
                )
                return module

        from tripy.backend.mlir.utils import redirect_stderr

        try:
            with redirect_stderr() as outfile:
                mlir = to_mlir_impl()
        except Exception as exc:
            from tripy.backend.mlir.utils import map_error_to_user_code_and_raise

            outfile.flush()
            outfile.seek(0)
            stderr = outfile.read()

            map_error_to_user_code_and_raise(self, exc, stderr.decode())

        return mlir

    def register_tensor(self, tensor: "FlatIRTensor") -> "FlatIRTensor":
        """
        Registers a tensor with this FlatIR instance. If the tensor has no name, a name unique to this FlatIR will be assigned.

        Args:
            tensor: The tensor to register.
        """
        tensor.name = utils.default(tensor.name, f"t_inter{len(self.tensor_map)}")
        if tensor.name in self.tensor_map:
            return self.tensor_map[tensor.name]
        self.tensor_map[tensor.name] = tensor
        return tensor

    def integrate_subgraph(self, inputs: List["FlatIRTensor"], outputs: List["FlatIRTensor"]):
        """
        Integrates a subgraph delineated by the given inputs and outputs into this FlatIR.
        """
        seen_tensors: Set[int] = set()
        ops = []

        # Implements dfs search
        def register_tensor_and_collect_ops(tensor, seen_tensors):
            if id(tensor) not in seen_tensors:
                seen_tensors.add(id(tensor))
                self.register_tensor(tensor)

                op = tensor.producer

                assert (
                    op is not None
                ), f"Tensor: {tensor} has no producer set. Did you use the constructor instead of the `build()` function?"

                for inp in op.inputs:
                    if inp not in inputs:
                        register_tensor_and_collect_ops(inp, seen_tensors)

                ops.append(op)

        for start_tensor in outputs:
            register_tensor_and_collect_ops(start_tensor, seen_tensors)

        # Rebind the ops to tensors from this FlatIR
        for op in ops:
            op.inputs = [self.register_tensor(inp) for inp in op.inputs]
            op.outputs = [self.register_tensor(out) for out in op.outputs]
            op.trace_input_names = [inp.name for inp in inputs]
            op.trace_output_names = [out.name for out in outputs]

        self.ops.extend(ops)
