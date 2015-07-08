import logging
from collections import OrderedDict

import numpy

import theano
from theano import tensor

from blocks import initialization
from blocks.bricks import Linear, Tanh, Softmax, FeedforwardSequence
from blocks.bricks.parallel import Fork
from blocks.bricks.recurrent import SimpleRecurrent, RecurrentStack

from rnn.bricks import LookupTable

floatX = theano.config.floatX
logging.basicConfig(level='INFO')
logger = logging.getLogger(__name__)


# TODO: clean this function, split it in several pieces maybe
def build_model_vanilla(vocab_size, args, dtype=floatX):
    logger.info('Building model ...')

    # Parameters for the model
    context = args.context
    state_dim = args.state_dim
    layers = args.layers
    skip_connections = args.skip_connections
    skip_output = args.skip_output

    # Symbolic variables
    # In both cases: Time X Batch
    x = tensor.lmatrix('features')
    y = tensor.lmatrix('targets')

    # Build the model
    output_names = []
    output_dims = []
    for d in range(layers):
        if d > 0:
            suffix = '_' + str(d)
        else:
            suffix = ''
        if d == 0 or skip_connections:
            output_names.append("inputs" + suffix)
            output_dims.append(state_dim)

    lookup = LookupTable(length=vocab_size, dim=state_dim)
    lookup.weights_init = initialization.IsotropicGaussian(0.1)
    lookup.biases_init = initialization.Constant(0)

    fork = Fork(output_names=output_names, input_dim=args.mini_batch_size,
                output_dims=output_dims,
                prototype=FeedforwardSequence(
                    [lookup.apply]))

    transitions = [SimpleRecurrent(dim=state_dim, activation=Tanh())
                   for _ in range(layers)]

    rnn = RecurrentStack(transitions, skip_connections=skip_connections)

    # If skip_connections: dim = layers * state_dim
    # else: dim = state_dim
    use_all_states = skip_connections or skip_output
    output_layer = Linear(
        input_dim=use_all_states * layers *
        state_dim + (1 - use_all_states) * state_dim,
        output_dim=vocab_size, name="output_layer")

    # Return list of 3D Tensor, one for each layer
    # (Time X Batch X embedding_dim)
    pre_rnn = fork.apply(x)

    # Give a name to the input of each layer
    if skip_connections:
        for t in range(len(pre_rnn)):
            pre_rnn[t].name = "pre_rnn_" + str(t)
    else:
        pre_rnn.name = "pre_rnn"

    # Prepare inputs for the RNN
    kwargs = OrderedDict()
    init_states = {}
    for d in range(layers):
        if d > 0:
            suffix = '_' + str(d)
        else:
            suffix = ''
        if skip_connections:
            kwargs['inputs' + suffix] = pre_rnn[d]
        elif d == 0:
            kwargs['inputs'] = pre_rnn
        init_states[d] = theano.shared(
            numpy.zeros((args.mini_batch_size, state_dim)).astype(floatX),
            name='state0_%d' % d)
        kwargs['states' + suffix] = init_states[d]

    # Apply the RNN to the inputs
    h = rnn.apply(low_memory=True, **kwargs)

    # We have
    # h = [state, state_1, state_2 ...] if layers > 1
    # h = state if layers == 1

    # If we have skip connections, concatenate all the states
    # Else only consider the state of the highest layer
    last_states = {}
    if layers > 1:
        # Save all the last states
        for d in range(layers):
            last_states[d] = h[d][-1, :, :]
        if skip_connections or skip_output:
            h = tensor.concatenate(h, axis=2)
        else:
            h = h[-1]
    else:
        last_states[0] = h[-1, :, :]
    h.name = "hidden_state"

    # The updates of the hidden states
    updates = []
    for d in range(layers):
        updates.append((init_states[d], last_states[d]))

    presoft = output_layer.apply(h[context:, :, :])
    # Define the cost
    # Compute the probability distribution
    time, batch, feat = presoft.shape
    presoft.name = 'presoft'

    cross_entropy = Softmax().categorical_cross_entropy(
        y[context:, :].flatten(),
        presoft.reshape((batch * time, feat)))
    cross_entropy = cross_entropy / tensor.log(2)
    cross_entropy.name = "cross_entropy"

    # TODO: add regularisation for the cost
    # the log(1) is here in order to differentiate the two variables
    # for monitoring
    cost = cross_entropy + tensor.log(1)
    cost.name = "regularized_cost"

    # Initialize the model
    logger.info('Initializing...')

    fork.initialize()

    rnn.weights_init = initialization.Orthogonal()
    rnn.biases_init = initialization.Constant(0)
    rnn.initialize()

    output_layer.weights_init = initialization.IsotropicGaussian(0.1)
    output_layer.biases_init = initialization.Constant(0)
    output_layer.initialize()

    return cost, cross_entropy, updates