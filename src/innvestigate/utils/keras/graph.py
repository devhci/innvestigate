from __future__ import annotations

import inspect
from abc import ABCMeta, abstractmethod
from builtins import range, zip
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import keras.backend as K
import keras.engine.topology
import keras.layers
import keras.models
import numpy as np
import six

import innvestigate.layers as ilayers
import innvestigate.utils as iutils
import innvestigate.utils.keras.checks as kchecks
from innvestigate.utils.keras import apply as kapply
from innvestigate.utils.types import (
    Layer,
    LayerCheck,
    Model,
    NodeDict,
    OptionalList,
    ReverseTensorDict,
    Tensor,
)

__all__ = [
    "get_kernel",
    "get_layer_inbound_count",
    "get_layer_neuronwise_io",
    "copy_layer_wo_activation",
    "copy_layer",
    "pre_softmax_tensors",
    "model_wo_softmax",
    "get_model_layers",
    "model_contains",
    "trace_model_execution",
    "get_model_execution_trace",
    "get_model_execution_graph",
    "print_model_execution_graph",
    "get_bottleneck_nodes",
    "get_bottleneck_tensors",
    "ReverseMappingBase",
    "reverse_model",
]


def get_kernel(layer: Layer) -> Tensor:
    """Returns the kernel weights of a layer, i.e, w/o biases."""
    ret = [x for x in layer.get_weights() if len(x.shape) > 1]
    assert len(ret) == 1
    return ret[0]


def get_input_layers(layer: Layer) -> Set[Layer]:
    """Returns all layers that created this layer's inputs."""
    ret = set()

    for node_index in range(len(layer._inbound_nodes)):
        Xs = iutils.to_list(layer.get_input_at(node_index))
        for X in Xs:
            ret.add(X._keras_history[0])

    return ret


###############################################################################


def get_layer_inbound_count(layer: Layer) -> int:
    """Returns the number inbound nodes of a layer."""
    return len(layer._inbound_nodes)


def get_layer_neuronwise_io(
    layer: Layer,
    node_index: int = 0,
    Xs: List[Tensor] = None,
    Ys: List[Tensor] = None,
    return_i: bool = True,
    return_o: bool = True,
) -> Union[Tuple[List[Tensor], List[Tensor]], List[Tensor]]:
    """Returns the input and output for each neuron in a layer

    Returns the symbolic input and output for each neuron in a layer.
    For a dense layer this is the input output itself.
    For convolutional layers this method extracts for each neuron
    the input output mapping.

    At the moment this function is designed
    to work with dense and conv2d layers.

    :param layer: The targeted layer.
    :param node_index: Index of the layer node to use.
    :param Xs: Ignore the layer's input but use Xs instead.
    :param Ys: Ignore the layer's output but use Ys instead.
    :param return_i: Return the inputs.
    :param return_o: Return the outputs.
    :return: Inputs and outputs, if specified, for each individual neuron.
    """
    if not kchecks.contains_kernel(layer):
        raise NotImplementedError()

    if Xs is None:
        Xs = iutils.to_list(layer.get_input_at(node_index))
    if Ys is None:
        Ys = iutils.to_list(layer.get_output_at(node_index))

    if isinstance(layer, keras.layers.Dense):
        # Xs and Ys are already in shape.
        ret_Xs = Xs
        ret_Ys = Ys
    elif isinstance(layer, keras.layers.Conv2D):
        kernel = get_kernel(layer)
        # Expect filter dimension to be last.
        n_channels = kernel.shape[-1]

        if return_i:
            extract_patches = ilayers.ExtractConv2DPatches(
                kernel.shape[:2],
                kernel.shape[2],
                layer.strides,
                layer.dilation_rate,
                layer.padding,
            )
            # shape [samples, out_row, out_col, weight_size]
            reshape = ilayers.Reshape((-1, np.product(kernel.shape[:3])))
            ret_Xs = [reshape(extract_patches(x)) for x in Xs]

        if return_o:
            # Get Ys into shape (samples, channels)
            if K.image_data_format() == "channels_first":
                # Ys shape is [samples, channels, out_row, out_col]
                def _reshape(x):
                    x = ilayers.Transpose((0, 2, 3, 1))(x)
                    x = ilayers.Reshape((-1, n_channels))(x)
                    return x

            else:
                # Ys shape is [samples, out_row, out_col, channels]
                def _reshape(x):
                    x = ilayers.Reshape((-1, n_channels))(x)
                    return x

            ret_Ys = [_reshape(x) for x in Ys]

    else:
        raise NotImplementedError()

    # Xs is (n, d) and Ys is (d, channels)
    if return_i and return_o:
        return ret_Xs, ret_Ys
    elif return_i:
        return ret_Xs
    elif return_o:
        return ret_Ys
    else:
        raise Exception()


def get_symbolic_weight_names(layer: Layer, weights: List[Tensor] = None) -> List[str]:
    """Attribute names for weights

    Looks up the attribute names of weight tensors.

    :param layer: Targeted layer.
    :param weights: A list of weight tensors.
    :return: The attribute names of the weights.
    """

    if weights is None:
        weights = layer.weights

    good_guesses = [
        "kernel",
        "bias",
        "gamma",
        "beta",
        "moving_mean",
        "moving_variance",
        "depthwise_kernel",
        "pointwise_kernel",
    ]

    ret = []
    for weight in weights:
        for attr_name in good_guesses + dir(layer):
            if hasattr(layer, attr_name) and id(weight) == id(
                getattr(layer, attr_name)
            ):
                ret.append(attr_name)
                break
    if len(weights) != len(ret):
        raise Exception("Could not find symoblic weight name(s).")

    return ret


def update_symbolic_weights(layer: Layer, weight_mapping: Dict[str, Tensor]) -> None:
    """Updates the symbolic tensors of a layer

    Updates the symbolic tensors of a layer by replacing them.

    Note this does not update the loss or anything alike!
    Use with caution!

    :param layer: Targeted layer.
    :param weight_mapping: Dict with attribute name and weight tensors
      as keys and values.
    """

    trainable_weight_ids = [id(x) for x in layer._trainable_weights]
    non_trainable_weight_ids = [id(x) for x in layer._non_trainable_weights]

    for name, weight in six.iteritems(weight_mapping):
        current_weight = getattr(layer, name)
        current_weight_id = id(current_weight)

        if current_weight_id in trainable_weight_ids:
            idx = trainable_weight_ids.index(current_weight_id)
            layer._trainable_weights[idx] = weight
        else:
            idx = non_trainable_weight_ids.index(current_weight_id)
            layer._non_trainable_weights[idx] = weight

        setattr(layer, name, weight)


def get_layer_from_config(
    old_layer: Layer,
    new_config: Dict[str, Any],
    weights: Optional[Union[List[np.ndarray], List[Tensor]]] = None,
    reuse_symbolic_tensors: bool = True,
) -> Layer:
    """Creates a new layer from a config

    Creates a new layer given a changed config and weights etc.

    :param old_layer: A layer that shall be used as base.
    :param new_config: The config to create the new layer.
    :param weights: Weights to set in the new layer.
      Options: np tensors, symbolic tensors, or None,
      in which case the weights from old_layers are used.
    :param reuse_symbolic_tensors: If the weights of the
      old_layer are used copy the symbolic ones or copy
      the Numpy weights.
    :return: The new layer instance.
    """
    new_layer = old_layer.__class__.from_config(new_config)

    if weights is None:
        if reuse_symbolic_tensors:
            weights = old_layer.weights
        else:
            weights = old_layer.get_weights()

    if len(weights) > 0:
        input_shapes = old_layer.get_input_shape_at(0)
        # todo: inspect and set initializers to something fast for speedup
        new_layer.build(input_shapes)

        is_np_weight = [isinstance(x, np.ndarray) for x in weights]
        if all(is_np_weight):
            new_layer.set_weights(weights)
        else:
            if any(is_np_weight):
                raise ValueError(
                    "Expect either all weights to be " "np tensors or symbolic tensors."
                )

            symbolic_names = get_symbolic_weight_names(old_layer)
            update = {name: weight for name, weight in zip(symbolic_names, weights)}
            update_symbolic_weights(new_layer, update)

    return new_layer


def copy_layer_wo_activation(
    layer: Layer,
    keep_bias: bool = True,
    name_template: Optional[str] = None,
    weights: Optional[Union[List[np.ndarray], List[Tensor]]] = None,
    reuse_symbolic_tensors: bool = True,
    **kwargs
) -> Layer:
    """Copy a Keras layer and remove the activations

    Copies a Keras layer but remove potential activations.

    :param layer: A layer that should be copied.
    :param keep_bias: Keep a potential bias.
    :param weights: Weights to set in the new layer.
      Options: np tensors, symbolic tensors, or None,
      in which case the weights from old_layers are used.
    :param reuse_symbolic_tensors: If the weights of the
      old_layer are used copy the symbolic ones or copy
      the Numpy weights.
    :return: The new layer instance.
    """
    config = layer.get_config()
    if name_template is None:
        config["name"] = None
    else:
        config["name"] = name_template % config["name"]
    if kchecks.contains_activation(layer):
        config["activation"] = None
    if hasattr(layer, "use_bias"):
        if keep_bias is False and config.get("use_bias", True):
            config["use_bias"] = False
            if weights is None:
                if reuse_symbolic_tensors:
                    weights = layer.weights[:-1]
                else:
                    weights = layer.get_weights()[:-1]
    return get_layer_from_config(layer, config, weights=weights, **kwargs)


def copy_layer(
    layer: Layer,
    keep_bias: bool = True,
    name_template: bool = None,
    weights: Optional[Union[List[Tensor], List[np.ndarray]]] = None,
    reuse_symbolic_tensors: bool = True,
    **kwargs
) -> Layer:
    """Copy a Keras layer.

    :param layer: A layer that should be copied.
    :param keep_bias: Keep a potential bias.
    :param weights: Weights to set in the new layer.
      Options: np tensors, symbolic tensors, or None,
      in which case the weights from old_layers are used.
    :param reuse_symbolic_tensors: If the weights of the
      old_layer are used copy the symbolic ones or copy
      the Numpy weights.
    :return: The new layer instance.
    """
    config = layer.get_config()
    if name_template is None:
        config["name"] = None
    else:
        config["name"] = name_template % config["name"]
    if hasattr(layer, "use_bias"):
        if keep_bias is False and config.get("use_bias", True):
            config["use_bias"] = False
            if weights is None:
                if reuse_symbolic_tensors:
                    weights = layer.weights[:-1]
                else:
                    weights = layer.get_weights()[:-1]
    return get_layer_from_config(layer, config, weights=weights, **kwargs)


def pre_softmax_tensors(Xs: Tensor, should_find_softmax: bool = True) -> List[Tensor]:
    """Finds the tensors that were preceeding a potential softmax."""
    softmax_found = False

    Xs = iutils.to_list(Xs)
    ret = []
    for x in Xs:
        layer, node_index, _tensor_index = x._keras_history
        if kchecks.contains_activation(layer, activation="softmax"):
            softmax_found = True
            if isinstance(layer, keras.layers.Activation):
                ret.append(layer.get_input_at(node_index))
            else:
                layer_wo_act = copy_layer_wo_activation(layer)
                ret.append(layer_wo_act(layer.get_input_at(node_index)))

    if should_find_softmax and not softmax_found:
        raise Exception("No softmax found.")

    return ret


def model_wo_softmax(model: Model) -> Model:
    """Creates a new model w/o the final softmax activation."""
    return keras.models.Model(
        inputs=model.inputs, outputs=pre_softmax_tensors(model.outputs), name=model.name
    )


###############################################################################


def get_model_layers(model: Model) -> List[Layer]:
    """Returns all layers of a model."""
    layers = []

    def collect_layers(container: Model) -> None:
        for layer in container.layers:
            assert layer not in layers
            layers.append(layer)
            if kchecks.is_network(layer):
                collect_layers(layer)

    collect_layers(model)

    return layers


def model_contains(
    model: Model,
    layer_condition: OptionalList[LayerCheck],
) -> List[List[Layer]]:
    """
    Collect layers in model which satisfy `layer_condition`.
    If multiple conditions are given in `layer_condition`,
    the collected layers are returned for each condition.

    :param model: A Keras model.
    :type model: Model
    :param layer_condition: A boolean function or list of functions that
        check Keras layers.
    :type layer_condition: Union[LayerCheck, List[LayerCheck]]
    :return: List, which for each condition in layer_condition
        contains a list of layers which satisfy that condition.
    :rtype: List[List[Layer]]
    """
    conditions = iutils.to_list(layer_condition)
    layers = get_model_layers(model)

    # return layers for which condition c holds true
    return [[l for l in layers if c(l)] for c in conditions]


###############################################################################


def apply_mapping_to_fused_bn_layer(mapping, fuse_mode: str = "one_linear") -> Callable:
    """
    Applies a mapping to a linearized Batch Normalization layer.

    :param mapping: The mapping to be applied.
      Should take parameters layer and reverse_state and
      return a mapping function.
    :param fuse_mode: Either 'one_linear': apply the mapping
      to a once linearized layer, or
      'two_linear': apply to twice to a twice linearized layer.
    """
    if fuse_mode not in ["one_linear", "two_linear"]:
        raise ValueError("fuse_mode can only be 'one_linear' or 'two_linear'")

    # TODO (alber): remove this workaround and make a proper class
    def ScaleLayer(kernel, bias):
        _kernel = kernel
        _bias = bias

        class ScaleLayer(keras.layers.Layer):
            def __init__(self, use_bias=True, **kwargs):
                self._kernel_to_be = _kernel
                self._bias_to_be = _bias
                self.use_bias = use_bias
                super().__init__(**kwargs)

            def build(self, input_shape):
                self.kernel = self.add_weight(
                    name="kernel",
                    shape=K.int_shape(self._kernel_to_be),
                    initializer=lambda a, b=None: self._kernel_to_be,
                    trainable=False,
                )
                if self.use_bias:
                    self.bias = self.add_weight(
                        name="bias",
                        shape=K.int_shape(self._bias_to_be),
                        initializer=lambda a, b=None: self._bias_to_be,
                        trainable=False,
                    )
                super().build(input_shape)

            def call(self, x):
                ret = x * self.kernel
                if self.use_bias:
                    ret += self.bias
                return ret

            def compute_output_shape(self, input_shape):
                return input_shape

        return ScaleLayer()

    def meta_mapping(layer: Layer, reverse_state: Dict):
        # get bn params
        weights = layer.weights[:]  # copy array
        if layer.scale:
            gamma = weights.pop(0)
        else:
            gamma = K.ones_like(weights[0])
        if layer.center:
            beta = weights.pop(0)
        else:
            beta = K.zeros_like(weights[0])
        mean, variance = weights

        if fuse_mode == "one_linear":
            tmp = K.sqrt(variance ** 2 + layer.epsilon)
            tmp_k = gamma / tmp
            tmp_b = -mean / tmp + beta

            inputs = layer.get_input_at(0)
            surrogate_layer = ScaleLayer(tmp_k, tmp_b)
            # init layer
            surrogate_layer(inputs)
            actual_mapping = mapping(surrogate_layer, reverse_state).apply
        else:
            tmp = K.sqrt(variance ** 2 + layer.epsilon)
            tmp_k1 = 1 / tmp
            tmp_b1 = -mean / tmp
            tmp_k2 = gamma
            tmp_b2 = beta

            inputs = layer.get_input_at(0)
            surrogate_layer1 = ScaleLayer(tmp_k1, tmp_b1)
            surrogate_layer2 = ScaleLayer(tmp_k2, tmp_b2)
            # init layers
            surrogate_layer1(inputs)
            surrogate_layer2(inputs)
            # TODO (alber): update reverse state
            actual_mapping_1 = mapping(surrogate_layer1, reverse_state).apply
            actual_mapping_2 = mapping(surrogate_layer2, reverse_state).apply

            def actual_mapping(
                Xs: List[Tensor],
                Ys: List[Tensor],
                reversed_Ys: List[Tensor],
                reverse_state,
            ):

                X2s = kapply(surrogate_layer1, Xs)
                # Apply first mapping
                # TODO (alber): update reverse state
                reversed_X2s = actual_mapping_2(X2s, Ys, reversed_Ys, reverse_state)
                return actual_mapping_1(Xs, X2s, reversed_X2s, reverse_state)

        return actual_mapping

    return meta_mapping


###############################################################################


def trace_model_execution(
    model: Model, reapply_on_copied_layers: bool = False
) -> Tuple[List[Layer], List[Tuple[Layer, List[Tensor], List[Tensor]]], List[Tensor]]:
    """
    Trace and linearize excecution of a model and it's possible containers.
    Return a triple with all layers, a list with a linearized execution
    with (layer, input_tensors, output_tensors), and, possible regenerated,
    outputs of the exectution.

    :param model: A kera model.
    :param reapply_on_copied_layers: If the execution needs to be linearized,
      reapply with copied layers. Might be slow. Prevents changes of the
      original layer's node lists.
    """

    # Get all layers in model.
    layers: List[Layer] = get_model_layers(model)

    # Check if some layers are containers.
    # Ignoring the outermost container, i.e. the passed model.
    contains_container: bool = any(
        [((l is not model) and kchecks.is_network(l)) for l in layers]
    )

    outputs: List[Tensor]

    # If so rebuild the graph, otherwise recycle computations,
    # and create executed node list. (Keep track of paths?)
    if contains_container is True:
        # When containers/models are used as layers, then layers
        # inside the container/model do not keep track of nodes.
        # This makes it impossible to iterate of the nodes list and
        # recover the input output tensors. (see else clause)
        #
        # To recover the computational graph we need to re-apply it.
        # This implies that the tensors-object we use for the forward
        # pass are different to the passed model. This it not the case
        # for the else clause.
        #
        # Note that reapplying the model does only change the inbound
        # and outbound nodes of the model itself. We copy the model
        # so the passed model should not be affected from the
        # reapplication.
        executed_nodes: List[Tuple[Layer, List[Tensor], List[Tensor]]] = []

        # Monkeypatch the call function in all the used layer classes.
        monkey_patches: List[Tuple[Layer, Callable]] = [
            (layer, layer.call) for layer in layers
        ]
        try:

            def patch(self, method: Callable):
                if hasattr(method, "__patched__") is True:
                    raise Exception(
                        "Should not happen as we patch objects, not classes."
                    )

                def f(*args, **kwargs):
                    input_tensors = args[0]
                    output_tensors = method(*args, **kwargs)
                    executed_nodes.append((self, input_tensors, output_tensors))
                    return output_tensors

                f.__patched__ = True  # type: ignore
                return f

            # Apply the patches.
            for layer in layers:
                layer.call = patch(layer, layer.call)

            # Trigger reapplication of model.
            model_copy: Model = keras.models.Model(
                inputs=model.inputs, outputs=model.outputs
            )
            outputs = iutils.to_list(model_copy(model.inputs))
        finally:
            # Revert the monkey patches
            for layer, old_method in monkey_patches:
                layer.call = old_method

        # Now we have the problem that all the tensors
        # do not have a keras_history attribute as they are not part
        # of any node. Apply the flat model to get it.

        tensor_mapping: Dict[Tensor, Tensor] = {tmp: tmp for tmp in model.inputs}
        layer_mapping: Dict[Layer, Layer]
        new_executed_nodes: List[Tuple[Layer, List[Tensor], List[Tensor]]] = []

        if reapply_on_copied_layers is True:
            layer_mapping = {layer: copy_layer(layer) for layer in layers}
        else:
            layer_mapping = {layer: layer for layer in layers}

        for layer, Xs, Ys in executed_nodes:
            layer = layer_mapping[layer]
            Xs, Ys = iutils.to_list(Xs), iutils.to_list(Ys)

            if isinstance(layer, keras.layers.InputLayer):
                # Special case. Do nothing.
                new_Xs, new_Ys = Xs, Ys
            else:
                new_Xs = [tensor_mapping[x] for x in Xs]
                new_Ys = iutils.to_list(kapply(layer, new_Xs))

            # Update values of Ys in tensor_mapping with new_Ys
            tensor_mapping.update({k: v for k, v in zip(Ys, new_Ys)})
            new_executed_nodes.append((layer, new_Xs, new_Ys))

        layers = [layer_mapping[layer] for layer in layers]
        outputs = [tensor_mapping[x] for x in outputs]
        executed_nodes = new_executed_nodes
    else:
        # Easy and safe way.
        reverse_executed_nodes: List[Tuple[Layer, List[Tensor], List[Tensor]]] = [
            (node.outbound_layer, node.input_tensors, node.output_tensors)
            for depth in sorted(model._nodes_by_depth.keys())
            for node in model._nodes_by_depth[depth]
        ]
        outputs = model.outputs

        executed_nodes = list(reversed(reverse_executed_nodes))

    # `executed_nodes` potentially contains nodes that are not part
    # of the final execution graph.
    # E.g. if a layer was also applied outside of the model. Then its
    # node list contains nodes that do not contribute to the model's output.
    # Those nodes are filtered here.
    used_as_input = [x for x in outputs]
    tmp = []
    for l, Xs, Ys in reversed(executed_nodes):
        if all([y in used_as_input for y in Ys]):
            used_as_input += Xs
            tmp.append((l, Xs, Ys))
    executed_nodes = list(reversed(tmp))

    return layers, executed_nodes, outputs


def get_model_execution_trace(
    model: Model,
    keep_input_layers: bool = False,
    reapply_on_copied_layers: bool = False,
) -> List[NodeDict]:
    """
    Returns a list representing the execution graph.
    Each key is the node's id as it is used by the reverse_model method.

    Each associated value contains a dictionary with the following items:

    * `nid`: the node id.
    * `layer`: the layer creating this node.
    * `Xs`: the input tensors (only valid if not in a nested container).
    * `Ys`: the output tensors (only valid if not in a nested container).
    * `Xs_nids`: the ids of the nodes creating the Xs.
    * `Ys_nids`: the ids of nodes using the according output tensor.
    * `Xs_layers`: the layer that created the according input tensor.
    * `Ys_layers`: the layers using the according output tensor.

    :param model: A kera model.
    :param keep_input_layers: Keep input layers.
    :param reapply_on_copied_layers: If the execution needs to be linearized,
      reapply with copied layers. Might be slow. Prevents changes of the
      original layer's node lists.
    """
    # Get execution_trace: list with a linearized execution
    # consisting of `(layer, input_tensors, output_tensors)`.
    execution_trace: List[Tuple[Layer, List[Tensor], List[Tensor]]]
    _, execution_trace, _ = trace_model_execution(
        model, reapply_on_copied_layers=reapply_on_copied_layers
    )

    # Enrich execution_trace with node ids to get id_execution_trace
    nid: Optional[int]
    current_nid: int
    id_execution_trace: List[Tuple[Optional[int], Layer, List[Tensor], List[Tensor]]]
    tmp: List[Tuple[Optional[int], Layer, List[Tensor], List[Tensor]]]

    current_nid = 0
    id_execution_trace = []
    for l, Xs, Ys in execution_trace:
        if isinstance(l, keras.layers.InputLayer):
            id_execution_trace.append((None, l, Xs, Ys))
        else:
            id_execution_trace.append((current_nid, l, Xs, Ys))
            current_nid += 1

    # Create lookups from tensor to creating or receiving layer-node
    inputs_to_node: Dict[int, List[int]]
    outputs_to_node: Dict[int, Optional[int]]

    inputs_to_node = {}
    outputs_to_node = {}
    for nid, _l, Xs, Ys in id_execution_trace:
        if nid is not None:
            for X in Xs:
                Xid = id(X)
                if Xid in inputs_to_node:
                    inputs_to_node[Xid].append(nid)
                else:
                    inputs_to_node[Xid] = [nid]

        if keep_input_layers or nid is not None:
            for Y in Ys:
                Yid = id(Y)
                if Yid in inputs_to_node:
                    raise Exception("Cannot be more than one creating node.")
                outputs_to_node[Yid] = nid

    # Enrich trace with this info.
    nid_to_nodes: Dict[Layer, Tuple[Optional[int], Layer, List[Tensor], List[Tensor]]]
    model_execution_trace: List[NodeDict]

    # TODO: fix invariance of type hints
    Xs_nids: List[Optional[int]]
    Ys_nids: List[Union[List[int], List[None]]]
    # Xs_layers = List[Layer]
    # Ys_layers = List[List[Layer]]

    nid_to_nodes = {t[0]: t for t in id_execution_trace}

    model_execution_trace = []
    for nid, l, Xs, Ys in id_execution_trace:

        if isinstance(l, keras.layers.InputLayer):
            # The nids that created or receive the tensors.
            Xs_nids = []  # Input layer does not receive.
            Ys_nids = [inputs_to_node[id(Y)] for Y in Ys]
            # The layers that created or receive the tensors.
            Xs_layers = []  # Input layer does not receive.
            Ys_layers = [
                [nid_to_nodes[Ynid][1] for Ynid in Ynids2] for Ynids2 in Ys_nids
            ]
        else:
            # The nids that created or receive the tensors.
            Xs_nids = [outputs_to_node.get(id(X), None) for X in Xs]
            Ys_nids = [inputs_to_node.get(id(Y), [None]) for Y in Ys]
            # The layers that created or receive the tensors.
            Xs_layers = [nid_to_nodes[Xnid][1] for Xnid in Xs_nids if Xnid is not None]
            Ys_layers = [
                [nid_to_nodes[Ynid][1] for Ynid in Ynids2 if Ynid is not None]
                for Ynids2 in Ys_nids
            ]

        entry: NodeDict = {
            "nid": nid,
            "layer": l,
            "Xs": Xs,
            "Ys": Ys,
            "Xs_nids": Xs_nids,
            "Ys_nids": Ys_nids,
            "Xs_layers": Xs_layers,
            "Ys_layers": Ys_layers,
        }
        model_execution_trace.append(entry)

    if not keep_input_layers:
        model_execution_trace = [
            tmp for tmp in model_execution_trace if tmp["nid"] is not None
        ]

    return model_execution_trace


def get_model_execution_graph(
    model: Model, keep_input_layers: bool = False
) -> Dict[Optional[int], Union[NodeDict, List[NodeDict]]]:
    """
    Returns a dictionary representing the execution graph.
    Each key is the node's id as it is used by the reverse_model method.

    Each associated value contains a dictionary with the following items:

    * `nid`: the node id.
    * `layer`: the layer creating this node.
    * `Xs`: the input tensors (only valid if not in a nested container).
    * `Ys`: the output tensors (only valid if not in a nested container).
    * `Xs_nids`: the ids of the nodes creating the Xs.
    * `Ys_nids`: the ids of nodes using the according output tensor.
    * `Xs_layers`: the layer that created the according input tensor.
    * `Ys_layers`: the layers using the according output tensor.

    :param model: A kera model.
    :param keep_input_layers: Keep input layers.
    """
    trace: List[NodeDict]
    input_layers: List[NodeDict]
    graph: Dict[Optional[int], Union[NodeDict, List[NodeDict]]]

    trace = get_model_execution_trace(
        model, keep_input_layers=keep_input_layers, reapply_on_copied_layers=False
    )

    # Input layers in graph have Node-ID `nid=None`.
    # Extract these from list of all nodes `trace`.
    input_layers = [node for node in trace if node["nid"] is None]

    # Create graph which maps Node-IDs to node information from full trace.
    graph = {node["nid"]: node for node in trace}
    if keep_input_layers:
        graph[None] = input_layers

    return graph


def print_model_execution_graph(
    graph: Dict[Optional[int], OptionalList[NodeDict]]
) -> None:
    """Pretty print of a model execution graph."""
    # TODO: check types

    def nids_as_str(nids: List[Optional[int]]) -> str:  # type: ignore
        return ", ".join(["%s" % nid for nid in nids])  # type: ignore

    def print_node(node) -> None:  # node of type NodeDict?
        print(
            "  [NID: %4s] [Layer: %20s] [Inputs from: %20s] [Outputs to: %20s]"
            % (
                node["nid"],
                node["layer"].name,
                nids_as_str(node["Xs_nids"]),
                nids_as_str(node["Ys_nids"]),
            )
        )

    if None in graph:  # Input layers in graph have Node-ID `None`
        print("Graph input layers:")
        for input_node in graph[None]:
            print_node(input_node)

    print("Graph nodes:")
    for nid in sorted([key for key in graph if key is not None]):
        if nid is None:
            continue
        print_node(graph[nid])


def get_bottleneck_nodes(
    inputs: List[Tensor],
    outputs: List[Tensor],
    execution_list: List[Tuple[Layer, List[Tensor], List[Tensor]]],
) -> List[Tuple[Layer, Tuple[List[Tensor], List[Tensor]]]]:
    """
    Given an execution list this function returns all nodes that
    are a bottleneck in the network, i.e., "all information" must pass
    through this node.
    """
    forward_connections: Dict[Tensor, List[Tensor]]
    open_connections: Dict[Tensor, bool]
    ret: List[Tuple[Layer, Tuple[List[Tensor], List[Tensor]]]]

    forward_connections = {}
    for l, Xs, Ys in execution_list:
        if isinstance(l, keras.layers.InputLayer):
            # Special case, do nothing.
            continue

        for x in Xs:
            if x in forward_connections:
                forward_connections[x] += Ys
            else:
                forward_connections[x] = list(Ys)

    open_connections = {}
    for X in inputs:
        for fw_c in forward_connections[X]:
            open_connections[fw_c] = True

    ret = []
    for l, Xs, Ys in execution_list:
        if isinstance(l, keras.layers.InputLayer):
            # Special case, do nothing.
            # Note: if a single input branches
            # this is not detected.
            continue

        for Y in Ys:
            assert Y in open_connections
            del open_connections[Y]

        if len(open_connections) == 0:
            ret.append((l, (Xs, Ys)))

        for Y in Ys:
            if Y not in outputs:
                for fw_c in forward_connections[Y]:
                    open_connections[fw_c] = True

    return ret


def get_bottleneck_tensors(
    inputs: List[Tensor],
    outputs: List[Tensor],
    execution_list: List[Tuple[Layer, List[Tensor], List[Tensor]]],
) -> List[Tensor]:
    """
    Given an execution list this function returns all tensors that
    are a bottleneck in the network, i.e., "all information" must pass
    through this tensor.
    """
    nodes: List[Tuple[Layer, Tuple[List[Tensor], List[Tensor]]]]
    nodes = get_bottleneck_nodes(inputs, outputs, execution_list)

    ret = []
    for _l, (Xs, Ys) in nodes:
        for tensor_list in (Xs, Ys):
            if len(tensor_list) == 1:
                tensor = tensor_list[0]
                if tensor not in ret:
                    ret.append(tensor)
            else:
                # TODO(albermax): put warning here?
                pass
    return ret


###############################################################################


class ReverseMappingBase(metaclass=ABCMeta):
    def __init__(self, layer: Layer, state: Dict[str, Any]) -> None:
        pass

    @abstractmethod
    def apply(
        self,
        Xs: List[Tensor],
        Ys: List[Tensor],
        Rs: List[Tensor],
        reverse_state: Dict,
    ) -> List[Tensor]:
        pass


def reverse_model(
    model: Model,
    reverse_mappings,  # TODO: type annotate reverse_mappings
    default_reverse_mapping: Optional[Callable] = None,
    head_mapping: Callable = None,
    stop_mapping_at_tensors: List[Tensor] = None,
    verbose: bool = False,
    return_all_reversed_tensors: bool = False,
    clip_all_reversed_tensors: Union[bool, Tuple[float, float]] = False,
    project_bottleneck_tensors: Union[bool, Tuple[float, float]] = False,
    execution_trace: Optional[
        Tuple[List[Layer], List[Tuple[Layer, List[Tensor], List[Tensor]]], List[Tensor]]
    ] = None,
    reapply_on_copied_layers: bool = False,
) -> Union[List[Tensor], Tuple[List[Tensor], Dict[Tensor, ReverseTensorDict]]]:
    """
    Reverses a Keras model based on the given reverse functions.
    It returns the reverted tensors for the according model inputs.

    :param model: A Keras model.
    :param reverse_mappings: Either a callable that matches layers to
      mappings or a dictionary with layers as keys and mappings as values.
      Allowed as mapping forms are:
          * A function of form (A) f(Xs, Ys, reversed_Ys, reverse_state).
          * A function of form f(B) f(layer, reverse_state) that returns
            a function of form (A).
          * A :class:`ReverseMappingBase` subclass.
    :param default_reverse_mapping: A function that reverses layers for
      which no mapping was given by param "reverse_mappings".
    :param head_mapping: Map output tensors to new values before passing
      them into the reverted network.
    :param stop_mapping_at_tensors: Tensors at which to stop the mapping.
      Similar to stop_gradient parameters for gradient computation.
    :param verbose: Print what's going on.
    :param return_all_reversed_tensors: Return all reverted tensors in addition
      to reverted model input tensors.
    :param clip_all_reversed_tensors: Clip each reverted tensor. False or tuple
      with min/max value.
    :param project_bottleneck_tensors: Project bottleneck layers in the
      reverting process into a given value range. False, True or (a, b) for
      projection range.
    :param reapply_on_copied_layers: When a model execution needs to
      linearized and copy layers before reapplying them. See
      :func:`trace_model_execution`.
    """

    # Set default values ######################################################
    if stop_mapping_at_tensors is None:
        stop_mapping_at_tensors = []

    if head_mapping is None:

        def head_mapping(X):
            return X

    if not callable(reverse_mappings):
        # not callable, assume a dict that maps from layer to mapping
        reverse_mapping_data = reverse_mappings

        def reverse_mappings(layer):
            try:
                return reverse_mapping_data[type(layer)]
            except KeyError:
                return None

    if clip_all_reversed_tensors is True:
        raise NotImplementedError(
            "Keyword argument `clip_all_reversed_tensors` ",
            "expected to be `False` or tuple with min/max values.",
        )

    def _print(s):
        if verbose is True:
            print(s)

    # Initialize structure that keeps track of reversed tensors
    # maps tensor to reverse tensor and additional node information
    reversed_tensors: Dict[Tensor, ReverseTensorDict]
    reversed_tensors = {}

    bottleneck_tensors = set()

    def add_reversed_tensors(nid, tensors_list, reversed_tensors_list) -> None:
        def add_reversed_tensor(i, X: Tensor, reversed_X: Tensor) -> None:
            # Do not keep tensors that should stop the mapping.
            if X in stop_mapping_at_tensors:  # type: ignore
                return

            if X not in reversed_tensors:  # no duplicate entries for forward tensors
                reversed_tensors[X] = {
                    "id": (nid, i),
                    "tensor": reversed_X,
                    "tensors": None,
                    "final_tensor": None,
                }
            else:
                tmp = reversed_tensors[X]  # tmp modifies reversed_tensors!
                if tmp["tensor"] is not None:
                    if tmp["tensors"] is not None:
                        raise Exception("Wrong order, tensors already aggregated!")

                    tmp["tensors"] = [tmp["tensor"], reversed_X]
                    tmp["tensor"] = None
                else:
                    raise Exception("Error during reverse tensor aggeregation.")

        tmp = zip(tensors_list, reversed_tensors_list)
        for i, (X, reversed_X) in enumerate(tmp):
            add_reversed_tensor(i, X, reversed_X)

    def get_reversed_tensor(tensor: Tensor) -> Tensor:
        tmp: ReverseTensorDict
        tmp = reversed_tensors[tensor]

        if tmp["final_tensor"] is None:
            if tmp["tensor"] is None:
                final_tensor = keras.layers.Add()(tmp["tensors"])
            else:
                final_tensor = tmp["tensor"]

            if project_bottleneck_tensors is not False:
                if tensor in bottleneck_tensors:
                    project = ilayers.Project(project_bottleneck_tensors)
                    final_tensor = project(final_tensor)

            if isinstance(clip_all_reversed_tensors, tuple):
                clip = ilayers.Clip(*clip_all_reversed_tensors)
                final_tensor = clip(final_tensor)

            tmp["final_tensor"] = final_tensor

        return tmp["final_tensor"]

    # Reverse the model #######################################################
    _print("Reverse model: {}".format(model))

    # Create a list with nodes in reverse execution order.
    if execution_trace is None:
        execution_trace = trace_model_execution(
            model, reapply_on_copied_layers=reapply_on_copied_layers
        )
    layers, execution_list, outputs = execution_trace
    len_execution_list = len(execution_list)
    num_input_layers = len(
        [_ for l, _, _ in execution_list if isinstance(l, keras.layers.InputLayer)]
    )
    len_execution_list_wo_inputs_layers = len_execution_list - num_input_layers
    reverse_execution_list = reversed(execution_list)

    # Initialize the reverse mapping functions.
    initialized_reverse_mappings: Dict[Layer, Callable]  # TODO: specify Callable
    initialized_reverse_mappings = {}
    for layer in layers:
        # A layer can be shared, i.e., applied several times.
        # Allow to share a ReverMappingBase for each layer instance
        # in order to reduce the overhead.

        meta_reverse_mapping = reverse_mappings(layer)
        if meta_reverse_mapping is None:
            reverse_mapping = default_reverse_mapping
        elif inspect.isclass(meta_reverse_mapping) and issubclass(
            meta_reverse_mapping, ReverseMappingBase
        ):
            # Mapping is a class
            reverse_mapping_obj = meta_reverse_mapping(
                layer,
                {
                    "model": model,
                    "layer": layer,
                },
            )
            reverse_mapping = reverse_mapping_obj.apply
        else:

            def parameter_count(func):
                if hasattr(inspect, "signature"):
                    ret = len(inspect.signature(func).parameters)
                else:
                    spec = inspect.getargspec(func)
                    ret = len(spec.args)
                    if spec.varargs is not None:
                        ret += len(spec.varargs)
                    if spec.keywords is not None:
                        ret += len(spec.keywords)
                    if ret == 3:
                        # assume class function with self
                        ret -= 1
                return ret

            if (
                callable(meta_reverse_mapping)
                and parameter_count(meta_reverse_mapping) == 2
            ):
                # Function that returns mapping
                reverse_mapping = meta_reverse_mapping(
                    layer,
                    {
                        "model": model,
                        "layer": layer,
                    },
                )
            else:
                # Nothing meta here
                reverse_mapping = meta_reverse_mapping

        initialized_reverse_mappings[layer] = reverse_mapping  # type: ignore # TODO: add annotations

    if project_bottleneck_tensors:
        bottleneck_tensors.update(
            get_bottleneck_tensors(model.inputs, outputs, execution_list)
        )

    # Initialize the reverse tensor mappings.
    add_reversed_tensors(-1, outputs, [head_mapping(tmp) for tmp in outputs])  # type: ignore

    # Follow the list and revert the graph.
    for _nid, (layer, Xs, Ys) in enumerate(reverse_execution_list):
        nid = len_execution_list_wo_inputs_layers - _nid - 1

        if isinstance(layer, keras.layers.InputLayer):
            # Special case. Do nothing.
            pass
        elif kchecks.is_network(layer):
            raise Exception("This is not supposed to happen!")
        else:
            Xs, Ys = iutils.to_list(Xs), iutils.to_list(Ys)
            if not all([ys in reversed_tensors for ys in Ys]):
                # This node is not part of our computational graph.
                # The (node-)world is bigger than this model.
                # Potentially this node is also not part of the
                # reversed tensor set because it depends on a tensor
                # that is listed in stop_mapping_at_tensors.
                continue
            reversed_Ys = [get_reversed_tensor(ys) for ys in Ys]
            local_stop_mapping_at_tensors = [
                x for x in Xs if x in stop_mapping_at_tensors
            ]

            _print("  [NID: {}] Reverse layer-node {}".format(nid, layer))
            reverse_mapping = initialized_reverse_mappings[layer]
            reversed_Xs = reverse_mapping(
                Xs,
                Ys,
                reversed_Ys,
                {
                    "nid": nid,
                    "model": model,
                    "layer": layer,
                    "stop_mapping_at_tensors": local_stop_mapping_at_tensors,
                },
            )
            reversed_Xs = iutils.to_list(reversed_Xs)
            add_reversed_tensors(nid, Xs, reversed_Xs)

    # Return requested values
    reversed_input_tensors = [
        get_reversed_tensor(tmp)
        for tmp in model.inputs
        if tmp not in stop_mapping_at_tensors
    ]
    if return_all_reversed_tensors is True:
        return reversed_input_tensors, reversed_tensors
    else:
        return reversed_input_tensors
