import logging
import os

import dill

import blocks
from examples.mnist import main as mnist_test
from examples.markov_chain.main import main as markov_chain_test


def setup():
    # Silence Block's logger
    logger = logging.getLogger(blocks.__name__)
    logger.setLevel(logging.ERROR)


def test_mnist():
    filename = 'mnist.pkl'
    mnist_test(filename, 1)
    with open(filename, "rb") as source:
        main_loop = dill.load(source)
    main_loop.find_extension("FinishAfter").invoke_after_n_epochs(2)
    main_loop.run()
    assert main_loop.log.status.epochs_done == 2
    os.remove(filename)

test_mnist.setup = setup


def test_markov_chain():
    filename = 'chain.pkl'
    markov_chain_test("train", filename, None, 10)
    os.remove(filename)

test_mnist.setup = setup