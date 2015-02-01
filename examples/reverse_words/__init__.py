import logging
import pprint
import sys
import math

import theano
from theano import tensor

from blocks.bricks import Tanh, application
from blocks.bricks.lookup import LookupTable
from blocks.bricks.recurrent import GatedRecurrent, Bidirectional
from blocks.bricks.attention import SequenceContentAttention
from blocks.bricks.parallel import Fork
from blocks.bricks.sequence_generators import (
    SequenceGenerator, LinearReadout, SoftmaxEmitter, LookupFeedback)
from blocks.graph import ComputationGraph
from blocks.datasets import (
    DataStreamMapping, BatchDataStream, PaddingDataStream,
    DataStreamFilter)
from blocks.datasets.text import OneBillionWord
from blocks.datasets.schemes import ConstantScheme
from blocks.algorithms import GradientDescent, SteepestDescent
from blocks.initialization import Orthogonal, IsotropicGaussian, Constant
from blocks.monitoring import aggregation
from blocks.extensions import FinishAfter, Printing, Timing
from blocks.extensions.saveload import SerializeMainLoop, LoadFromDump
from blocks.extensions.monitoring import TrainingDataMonitoring
from blocks.main_loop import MainLoop
from blocks.select import Selector
from blocks.filter import VariableFilter
from blocks.utils import named_copy, unpack, dict_union

sys.setrecursionlimit(100000)
floatX = theano.config.floatX
logger = logging.getLogger(__name__)


class Transition(GatedRecurrent):
    def __init__(self, attended_dim, **kwargs):
        super(Transition, self).__init__(**kwargs)
        self.attended_dim = attended_dim

    @application(contexts=['attended', 'attended_mask'])
    def apply(self, *args, **kwargs):
        for context in Transition.apply.contexts:
            kwargs.pop(context)
        return super(Transition, self).apply(*args, **kwargs)

    @apply.delegate
    def apply_delegate(self):
        return super(Transition, self).apply

    def get_dim(self, name):
        if name == 'attended':
            return self.attended_dim
        if name == 'attended_mask':
            return 0
        return super(Transition, self).get_dim(name)


def main(mode, save_path, num_batches, from_dump):
    if mode == "train":
        # Experiment configuration
        chars = ([chr(ord('a') + i) for i in range(26)] +
                 [chr(ord('0') + i) for i in range(10)] +
                 [',', '.', '!', '?', '<UNK>'] +
                 [' ', '<S>', '</S>'])
        code2char = dict(enumerate(chars))
        char2code = {v: k for k, v in code2char.items()}
        dimension = 100
        readout_dimension = len(chars)

        # Data processing pipeline
        def reverse_words(sample):
            sentence = sample[0]
            result = []
            word_start = -1
            for i, code in enumerate(sentence):
                if code >= char2code[' ']:
                    if word_start >= 1:
                        result.extend(sentence[i:word_start - 1:-1])
                        word_start = -1
                else:
                    if word_start == -1:
                        word_start = i
            return (result[1:],)

        data_stream = DataStreamMapping(
            mapping=lambda data: tuple(array.T for array in data),
            data_stream=PaddingDataStream(
                BatchDataStream(
                    iteration_scheme=ConstantScheme(10),
                    data_stream=DataStreamMapping(
                        mapping=reverse_words,
                        add_sources=("targets",),
                        data_stream=DataStreamFilter(
                            predicate=lambda data: len(data[0]) <= 100,
                            data_stream=OneBillionWord(
                                "training", [99], char2code,
                                level="character", preprocess=str.lower)
                            .get_default_stream())))))

        # Build the model
        chars = tensor.lmatrix("features")
        chars_mask = tensor.matrix("features_mask")
        targets = tensor.lmatrix("targets")
        targets_mask = tensor.matrix("targets_mask")

        encoder = Bidirectional(
            GatedRecurrent(dim=dimension, activation=Tanh()),
            weights_init=Orthogonal())
        encoder.initialize()
        fork = Fork([name for name in encoder.prototype.apply.sequences
                     if name != 'mask'],
                    weights_init=IsotropicGaussian(0.1),
                    biases_init=Constant(0))
        fork.input_dim = dimension
        fork.fork_dims = {name: dimension for name in fork.fork_names}
        fork.initialize()
        lookup = LookupTable(readout_dimension, dimension,
                             weights_init=IsotropicGaussian(0.1))
        lookup.initialize()
        transition = Transition(
            activation=Tanh(),
            dim=dimension, attended_dim=2 * dimension, name="transition")
        attention = SequenceContentAttention(
            state_names=transition.apply.states,
            match_dim=dimension, name="attention")
        readout = LinearReadout(
            readout_dim=readout_dimension, source_names=["states"],
            emitter=SoftmaxEmitter(name="emitter"),
            feedbacker=LookupFeedback(readout_dimension, dimension),
            name="readout")
        generator = SequenceGenerator(
            readout=readout, transition=transition, attention=attention,
            weights_init=IsotropicGaussian(0.1), biases_init=Constant(0),
            name="generator")
        generator.push_initialization_config()
        transition.weights_init = Orthogonal()
        generator.initialize()
        bricks = [encoder, fork, lookup, generator]

        # Give an idea of what's going on.
        params = Selector(bricks).get_params()
        logger.info("Parameters:\n" +
                    pprint.pformat(
                        [(key, value.get_value().shape) for key, value
                         in params.items()],
                        width=120))

        # Build the cost computation graph.
        batch_cost = generator.cost(
            targets, targets_mask,
            attended=encoder.apply(
                **dict_union(
                    fork.apply(lookup.lookup(chars), return_dict=True),
                    mask=chars_mask)),
            attended_mask=chars_mask).sum()
        batch_size = named_copy(chars.shape[1], "batch_size")
        cost = aggregation.mean(batch_cost,  batch_size)
        cost.name = "sequence_log_likelihood"
        logger.info("Cost graph is built")

        # Fetch variables useful for debugging
        max_length = named_copy(chars.shape[0], "max_length")
        cost_per_character = named_copy(
            aggregation.mean(batch_cost, batch_size * max_length),
            "character_log_likelihood")
        cg = ComputationGraph(cost)
        energies = unpack(
            VariableFilter(application=readout.readout, name="output")(
                cg.variables),
            singleton=True)
        min_energy = named_copy(energies.min(), "min_energy")
        max_energy = named_copy(energies.max(), "max_energy")
        (activations,) = VariableFilter(
            application=generator.transition.apply,
            name="states")(cg.variables)
        (inputs,) = VariableFilter(
            application=generator.transition.apply,
            name="inputs")(cg.variables)
        (weights,) = VariableFilter(
            application=generator.cost, name="weights")(cg.variables)
        (attended,) = VariableFilter(
            application=generator.cost, name="attended$")(cg.variables)

        # Define the training algorithm.
        algorithm = GradientDescent(
            cost=cost, step_rule=SteepestDescent(0.001))

        observables = [
            cost, min_energy, max_energy, algorithm.total_gradient_norm,
            batch_size, max_length, cost_per_character]
        for name, param in params.items():
            observables.append(named_copy(
                param.norm(2), name + "_norm"))
            observables.append(named_copy(
                algorithm.gradients[param].norm(2), name + "_grad_norm"))

        main_loop = MainLoop(
            model=bricks,
            data_stream=data_stream,
            algorithm=algorithm,
            extensions=([LoadFromDump(from_dump)] if from_dump else []) +
            [Timing(),
                TrainingDataMonitoring(observables, after_every_batch=True),
                FinishAfter(after_n_batches=num_batches)
                .add_condition(
                    "after_batch",
                    lambda log:
                        math.isnan(log.current_row.total_gradient_norm)),
                SerializeMainLoop(save_path, every_n_batches=500),
                Printing(every_n_batches=1)])
        main_loop.run()
    elif mode == "sample":
        raise NotImplementedError()
    else:
        assert False