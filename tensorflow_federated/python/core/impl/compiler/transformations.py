# Lint as: python3
# Copyright 2019, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Contains composite transformations, upon which higher compiler levels depend."""

from typing import Mapping

from tensorflow_federated.python.common_libs import py_typecheck
from tensorflow_federated.python.core.impl import transformations
from tensorflow_federated.python.core.impl import type_utils
from tensorflow_federated.python.core.impl.compiler import building_block_factory
from tensorflow_federated.python.core.impl.compiler import building_blocks
from tensorflow_federated.python.core.impl.compiler import transformation_utils


def prepare_for_rebinding(comp):
  """Prepares `comp` for extracting rebound variables.

  Currently, this means replacing all called lambdas and inlining all blocks.
  This does not necessarly guarantee that the resulting computation has no
  called lambdas, it merely reduces a level of indirection here. This reduction
  has proved sufficient for identifying variables which are about to be rebound
  in the top-level lambda, necessarily when compiler components factor work out
  from a single function into multiple functions. Since this function makes no
  guarantees about sufficiency, it is the responsibility of the caller to
  ensure that no unbound variables are introduced during the rebinding.

  Args:
    comp: Instance of `building_blocks.ComputationBuildingBlock` from which all
      occurrences of a given variable need to be extracted and rebound.

  Returns:
    Another instance of `building_blocks.ComputationBuildingBlock` which has
    had all called lambdas replaced by blocks, all blocks inlined and all
    selections from tuples collapsed.
  """
  # TODO(b/146430051): Follow up here and consider removing or enforcing more
  # strict output invariants when `remove_lambdas_and_blocks` is moved in here.
  py_typecheck.check_type(comp, building_blocks.ComputationBuildingBlock)
  comp, _ = transformations.uniquify_reference_names(comp)
  comp, _ = transformations.replace_called_lambda_with_block(comp)
  block_inliner = transformations.InlineBlock(comp)
  selection_replacer = transformations.ReplaceSelectionFromTuple()
  transforms = [block_inliner, selection_replacer]
  symbol_tree = transformation_utils.SymbolTree(
      transformation_utils.ReferenceCounter)

  def _transform_fn(comp, symbol_tree):
    """Transform function chaining inlining and collapsing selections."""
    modified = False
    for transform in transforms:
      if transform.global_transform:
        comp, transform_modified = transform.transform(comp, symbol_tree)
      else:
        comp, transform_modified = transform.transform(comp)
      modified = modified or transform_modified
    return comp, modified

  return transformation_utils.transform_postorder_with_symbol_bindings(
      comp, _transform_fn, symbol_tree)


def remove_lambdas_and_blocks(comp):
  """Removes any called lambdas and blocks from `comp`.

  This function will rename all the variables in `comp` in a single walk of the
  AST, then replace called lambdas with blocks in another walk, since this
  transformation interacts with scope in delicate ways. It will chain inlining
  the blocks and collapsing the selection-from-tuple pattern together into a
  final pass.

  Args:
    comp: Instance of `building_blocks.ComputationBuildingBlock` from which we
      want to remove called lambdas and blocks.

  Returns:
    A transformed version of `comp` which has no called lambdas or blocks, and
    no extraneous selections from tuples.
  """
  py_typecheck.check_type(comp, building_blocks.ComputationBuildingBlock)

  # TODO(b/146904968): In general, any bounded number of passes of these
  # transforms as currently implemented is insufficient in order to satisfy
  # the purpose of this function. Filing a new bug to followup if this becomes a
  # pressing issue.
  modified = False
  for fn in [
      transformations.remove_unused_block_locals,
      transformations.inline_selections_from_tuple,
      transformations.replace_called_lambda_with_block,
  ] * 2:
    comp, inner_modified = fn(comp)
    modified = inner_modified or modified
  for fn in [
      transformations.remove_unused_block_locals,
      transformations.uniquify_reference_names,
  ]:
    comp, inner_modified = fn(comp)
    modified = inner_modified or modified

  block_inliner = transformations.InlineBlock(comp)
  selection_replacer = transformations.ReplaceSelectionFromTuple()
  transforms = [block_inliner, selection_replacer]

  def _transform_fn(comp, symbol_tree):
    """Transform function chaining inlining and collapsing selections.

    This function is inlined here as opposed to factored out and parameterized
    by the transforms to apply, due to the delicacy of chaining transformations
    which rely on state. These transformations should be safe if they appear
    first in the list of transforms, but due to the difficulty of reasoning
    about the invariants the transforms can rely on in this setting, there is
    no function exposed which hoists out the internal logic.

    Args:
      comp: Instance of `building_blocks.ComputationBuildingBlock` we wish to
        check for inlining and collapsing of selections.
      symbol_tree: Instance of `building_blocks.SymbolTree` defining the
        bindings available to `comp`.

    Returns:
      A transformed version of `comp`.
    """
    modified = False
    for transform in transforms:
      if transform.global_transform:
        comp, transform_modified = transform.transform(comp, symbol_tree)
      else:
        comp, transform_modified = transform.transform(comp)
      modified = modified or transform_modified
    return comp, modified

  symbol_tree = transformation_utils.SymbolTree(
      transformation_utils.ReferenceCounter)
  transformed_comp, inner_modified = transformation_utils.transform_postorder_with_symbol_bindings(
      comp, _transform_fn, symbol_tree)
  modified = modified or inner_modified
  return transformed_comp, modified


def construct_tensorflow_calling_lambda_on_concrete_arg(
    parameter: building_blocks.Reference,
    body: building_blocks.ComputationBuildingBlock,
    concrete_arg: building_blocks.ComputationBuildingBlock):
  """Generates TensorFlow for lambda invocation with given arg, body and param.

  That is, generates TensorFlow block encapsulating the logic represented by
  invoking a function with parameter `parameter` and body `body`, with argument
  `concrete_arg`.

  Via the guarantee made in `compiled_computation_transforms.TupleCalledGraphs`,
  this function makes the claim that the computations which define
  `concrete_arg` will be executed exactly once in the generated TenosorFlow.

  Args:
    parameter: Instance of `building_blocks.Reference` defining the parameter of
      the function to be generated and invoked, as described above. After
      calling this transformation, every instance of  parameter` in `body` will
      represent a reference to `concrete_arg`.
    body: `building_blocks.ComputationBuildingBlock` representing the body of
      the function for which we are generating TensorFlow.
    concrete_arg: `building_blocks.ComputationBuildingBlock` representing the
      argument to be passed to the resulting function. `concrete_arg` will then
      be referred to by every occurrence of `parameter` in `body`. Therefore
      `concrete_arg` must have an equivalent type signature to that of
      `parameter`.

  Returns:
    A called `building_blocks.CompiledComputation`, as specified above.

  Raises:
    TypeError: If the arguments are of the wrong types, or the type signature
      of `concrete_arg` does not match that of `parameter`.
  """
  py_typecheck.check_type(parameter, building_blocks.Reference)
  py_typecheck.check_type(body, building_blocks.ComputationBuildingBlock)
  py_typecheck.check_type(concrete_arg,
                          building_blocks.ComputationBuildingBlock)
  type_utils.check_equivalent_types(parameter.type_signature,
                                    concrete_arg.type_signature)

  def _generate_simple_tensorflow(comp):
    tf_parser_callable = transformations.TFParser()
    comp, _ = transformations.insert_called_tf_identity_at_leaves(comp)
    comp, _ = transformation_utils.transform_postorder(comp, tf_parser_callable)
    return comp

  encapsulating_lambda = _generate_simple_tensorflow(
      building_blocks.Lambda(parameter.name, parameter.type_signature, body))
  comp_called = _generate_simple_tensorflow(
      building_blocks.Call(encapsulating_lambda, concrete_arg))
  return comp_called


def _replace_references_in_comp_with_selections_from_arg(
    comp: building_blocks.ComputationBuildingBlock,
    arg_ref: building_blocks.Reference, name_to_output_index: Mapping[str,
                                                                      int]):
  """Uses `name_to_output_index` to rebind references in `comp`."""

  def _replace_values_with_selections(inner_comp):
    if isinstance(inner_comp, building_blocks.Reference):
      selected_index = name_to_output_index[inner_comp.name]
      return building_blocks.Selection(
          source=arg_ref, index=selected_index), True
    return inner_comp, False

  comp_replaced, _ = transformation_utils.transform_postorder(
      comp, _replace_values_with_selections)
  return comp_replaced


def _construct_tensorflow_representing_single_local_assignment(
    arg_ref, arg_class, previous_output, name_to_output_index):
  """Constructs TensorFlow to represent assignment to a block local in sequence.

  Creates a tuple which represents all computations in the block local sequence
  depending on those variables which have already been processed, by combining
  the elements of `previous_output` with the computations in `arg_class`. Then
  generates TensorFlow to capture the logic this tuple encapsulates.

  Args:
    arg_ref: `building_blocks.Reference` to use in representing
      `previous_output` inside the body of the Lambda to be parsed to
      TensorFlow. Notice that this is here for name safety.
    arg_class: `list` of `building_blocks.ComputationBuildingBlock`s which are
      dependent on the block local being processed or any preceding block local;
      this should be one of the classes resulting from
      `group_block_locals_by_namespace`.
    previous_output: The result of parsing previous block local bindings into
      functions in the same manner.
    name_to_output_index: `dict` mapping block local variables to their index in
      the result of the generated TensorFlow. This is used to resolve references
      in the computations of `arg_class`, but will not be modified.

  Returns:
    Called instance of `building_blocks.CompiledComputation` representing
    the tuple described above.
  """
  pass_through_args = [
      building_blocks.Selection(source=arg_ref, index=idx)
      for idx, _ in enumerate(previous_output.type_signature)
  ]

  vals_replaced = [
      _replace_references_in_comp_with_selections_from_arg(
          c, arg_ref, name_to_output_index) for c in arg_class
  ]
  return_tuple = building_blocks.Tuple(pass_through_args + vals_replaced)

  comp_called = construct_tensorflow_calling_lambda_on_concrete_arg(
      arg_ref, return_tuple, previous_output)
  return comp_called


def _get_unbound_ref(block):
  """Helper to get unbound ref name and type spec if it exists in `block`."""
  all_unbound_refs = transformation_utils.get_map_of_unbound_references(block)
  top_level_unbound_ref = all_unbound_refs[block]
  num_unbound_refs = len(top_level_unbound_ref)
  if num_unbound_refs == 0:
    return None
  elif num_unbound_refs > 1:
    raise ValueError('`create_tensorflow_representing_block` must be passed '
                     'a block with at most a single unbound reference; '
                     'encountered the block {} with {} unbound '
                     'references.'.format(block, len(top_level_unbound_ref)))

  unbound_ref_name = top_level_unbound_ref.pop()

  top_level_type_spec = None

  def _get_unbound_ref_type_spec(inner_comp):
    if (isinstance(inner_comp, building_blocks.Reference) and
        inner_comp.name == unbound_ref_name):
      nonlocal top_level_type_spec
      top_level_type_spec = inner_comp.type_signature
    return inner_comp, False

  transformation_utils.transform_postorder(block, _get_unbound_ref_type_spec)
  return building_blocks.Reference(unbound_ref_name, top_level_type_spec)


def _check_parameters_for_tf_block_generation(block):
  """Helper to validate parameters for parsing block locals into TF graphs."""
  py_typecheck.check_type(block, building_blocks.Block)
  for _, comp in block.locals:
    if not (isinstance(comp, building_blocks.Call) and
            isinstance(comp.function, building_blocks.CompiledComputation)):
      raise ValueError(
          'create_tensorflow_representing_block may only be called '
          'on a block whose local variables are all bound to '
          'called TensorFlow computations; encountered a local '
          'bound to {}'.format(comp))

  def _check_contains_only_refs_sels_and_tuples(inner_comp):
    if not isinstance(inner_comp,
                      (building_blocks.Reference, building_blocks.Selection,
                       building_blocks.Tuple)):
      raise ValueError(
          'create_tensorflow_representing_block may only be called '
          'on a block whose result contains only Selections, '
          'Tuples and References; encountered the building block '
          '{}.'.format(inner_comp))
    return inner_comp, False

  transformation_utils.transform_postorder(
      block.result, _check_contains_only_refs_sels_and_tuples)


def create_tensorflow_representing_block(block):
  """Generates non-duplicated TensorFlow for Block locals binding called graphs.

  Assuming that the argument `block` satisfies the following conditions:

  1. The local variables in `block` are all called graphs, with arbitrary
      arguments.
  2. The result of the Block contains tuples, selections and references,
     but nothing else.

  Then `create_tensorflow_representing_block` will generate a structure, which
  may contain tensorflow functions, calls to tensorflow functions, and
  references, but which have generated this TensorFlow code without duplicating
  work done by referencing the block locals.

  Args:
    block: Instance of `building_blocks.Block`, whose local variables are all
      called instances of `building_blocks.CompiledComputation`, and whose
      result contains only instances of `building_blocks.Reference`,
      `building_blocks.Selection` or `building_blocks.Tuple`.

  Returns:
    A transformed version of `block`, which has pushed references to the called
    graphs in the locals of `block` into TensorFlow.

  Raises:
    TypeError: If `block` is not an instance of `building_blocks.Block`.
    ValueError: If the locals of `block` are anything other than called graphs,
      or if the result of `block` contains anything other than selections,
      references and tuples.
  """
  _check_parameters_for_tf_block_generation(block)

  name_generator = building_block_factory.unique_name_generator(block)

  def _construct_reference_representing(comp_to_represent):
    """Helper closing over `name_generator` for name safety."""
    arg_type = comp_to_represent.type_signature
    arg_name = next(name_generator)
    return building_blocks.Reference(arg_name, arg_type)

  top_level_ref = _get_unbound_ref(block)
  named_comp_classes = transformations.group_block_locals_by_namespace(block)

  if top_level_ref:
    first_comps = [x[1] for x in named_comp_classes[0]]
    tup = building_blocks.Tuple([top_level_ref] + first_comps)
    output_comp = construct_tensorflow_calling_lambda_on_concrete_arg(
        top_level_ref, tup, top_level_ref)
    name_to_output_index = {top_level_ref.name: 0}
  else:
    output_comp = building_block_factory.create_compiled_empty_tuple()
    name_to_output_index = {}

  block_local_names = [x[0] for x in block.locals]

  def _update_name_to_output_index(name_class):
    """Helper closing over `name_to_output_index` and `block_local_names`."""
    offset = len(name_to_output_index.keys())
    for idx, comp_name in enumerate(name_class):
      for var_name in block_local_names:
        if var_name == comp_name:
          name_to_output_index[var_name] = idx + offset

  if top_level_ref:
    first_names = [x[0] for x in named_comp_classes[0]]
    _update_name_to_output_index(first_names)
    remaining_comp_classes = named_comp_classes[1:]
  else:
    remaining_comp_classes = named_comp_classes[:]

  for named_comp_class in remaining_comp_classes:
    if named_comp_class:
      comp_class = [x[1] for x in named_comp_class]
      name_class = [x[0] for x in named_comp_class]
      arg_ref = _construct_reference_representing(output_comp)
      output_comp = _construct_tensorflow_representing_single_local_assignment(
          arg_ref, comp_class, output_comp, name_to_output_index)
      _update_name_to_output_index(name_class)

  arg_ref = _construct_reference_representing(output_comp)
  result_replaced = _replace_references_in_comp_with_selections_from_arg(
      block.result, arg_ref, name_to_output_index)
  comp_called = construct_tensorflow_calling_lambda_on_concrete_arg(
      arg_ref, result_replaced, output_comp)

  return comp_called, True
