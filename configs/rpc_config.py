from logging import INFO
from utils.concurrency.generic_node import GenericNode
from utils.concurrency.py_queue import PyQueue
from utils.confort import BaseConfig


class Logging(BaseConfig):
    level = INFO


class Network(BaseConfig):
    node = GenericNode

    class Args:
        in_queues = {
            'from_segmentation': PyQueue(ip="localhost", port=50000, queue_name='seg_to_rpc', blocking=False),
            'from_grasp_detection': PyQueue(ip="localhost", port=50000, queue_name='gd_to_rpc', blocking=False),
        }
        
class RPC:
    port_name = '/eCubPerception/rpc:i'
