'''
    Various utility functions used (possibly) across scripts.

    2022 Benjamin Kellenberger
'''

import random
import time
import torch
from torch.backends import cudnn


def init_seed(seed):
    '''
        Initalizes the seed for all random number generators used. This is
        important to be able to reproduce results and experiment with different
        random setups of the same code and experiments.
    '''
    if seed is not None:
        random.seed(seed)
        # numpy.random.seed(seed)       # we don't use NumPy in this code, but you would want to set its random number generator seed, too
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        cudnn.benchmark = True
        cudnn.deterministic = True

def time_sync():
    '''
        Pytorch-accurate time. See: https://discuss.pytorch.org/t/how-to-measure-time-in-pytorch/26964
    '''
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    return time.time()